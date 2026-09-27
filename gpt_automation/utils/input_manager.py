"""
Input manager for 1:1 email:card allocation through SQLite state.

Source text/CSV files remain read-only inputs. Resource leases and
disposition are owned exclusively by :class:`StateStore`.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import logging
import os
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from gpt_automation.state import ResourceConflictError, ResourceState, StateStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResourceAvailability:
    """Source-relative counts for one resource type."""

    total: int
    available: int
    reserved: int
    consumed: int
    quarantined: int

    @property
    def blocked(self) -> int:
        return self.reserved + self.quarantined


@dataclass(frozen=True)
class ResourceReport:
    """Detailed resource state used by startup and batch reporting."""

    email: ResourceAvailability
    card: ResourceAvailability

    @property
    def ready(self) -> bool:
        return self.email.available > 0 and self.card.available > 0

class InputManager:
    """Thread-safe input manager with 1:1 email:card consumption.

    Manages input files and ensures atomic allocation of email+card pairs
    for ChatGPT account registration.
    """

    def __init__(
        self,
        input_dir: str = "input",
        *,
        state_store: StateStore,
    ) -> None:
        """Initialize the InputManager.

        Args:
            input_dir: Directory containing input files. Defaults to "input".
            state_store: Required SQLite resource-lease store.
        """
        self.input_dir = input_dir
        self.email_file = os.path.join(input_dir, "email.txt")
        self.cards_file = os.path.join(input_dir, "cards.csv")
        self.address_file = os.path.join(input_dir, "address_jp.csv")
        self.used_email_file = os.path.join(input_dir, "used_email.txt")
        self.used_cards_file = os.path.join(input_dir, "used_cards.txt")
        self._state_store = state_store
        self._lock = asyncio.Lock()

        # Ensure input directory exists
        os.makedirs(input_dir, exist_ok=True)

        # Existing text usage files are imported once and then remain untouched.
        self._state_store.import_legacy_used_files(
            self.used_email_file,
            self.used_cards_file,
        )

    def check_resources(self) -> Tuple[bool, bool]:
        """Check if email and cards are available.

        Returns:
            Tuple of (email_available, card_available).
        """
        report = self.get_resource_report()
        return report.email.available > 0, report.card.available > 0

    def get_resource_report(self) -> ResourceReport:
        """Return counts that distinguish depletion from temporary blocking."""
        email_values = []
        for line in self._source_email_lines():
            parsed = self._parse_email_line(line)
            if parsed is not None:
                email_values.append(parsed["email"])
        card_values = []
        for row in self._source_card_rows():
            parsed = self._parse_card_line(row)
            if parsed is not None:
                card_values.append(parsed["card_no"])
        return ResourceReport(
            email=self._availability_for("email", email_values),
            card=self._availability_for("card", card_values),
        )

    def _availability_for(
        self,
        resource_type: str,
        values: List[str],
    ) -> ResourceAvailability:
        counts = {
            ResourceState.RESERVED: 0,
            ResourceState.CONSUMED: 0,
            ResourceState.QUARANTINED: 0,
        }
        available = 0
        for value in values:
            state = self._state_store.get_resource_state(resource_type, value)
            if state is None or state is ResourceState.RELEASED:
                available += 1
            elif state in counts:
                counts[state] += 1
        return ResourceAvailability(
            total=len(values),
            available=available,
            reserved=counts[ResourceState.RESERVED],
            consumed=counts[ResourceState.CONSUMED],
            quarantined=counts[ResourceState.QUARANTINED],
        )

    def get_resource_summary(self) -> Dict[str, int]:
        """Return remaining resource counts for batch reporting.

        Returns:
            Dict with keys: remaining_emails, remaining_cards
        """
        report = self.get_resource_report()
        return {
            "remaining_emails": report.email.available,
            "remaining_cards": report.card.available,
        }

    async def acquire_pair(self) -> Optional[Dict[str, str]]:
        """Atomically acquire one email + one card.

        Uses asyncio.Lock to ensure no race condition in async context.

        Returns:
            Dict with keys:
                - email: Email address
                - email_pwd: Email password (for IMAP)
                - client_id: UUID from email.csv
                - refresh_token: Refresh token from email.csv
                - card_no: Credit card number
                - card_expiry: Card expiry date
                - card_cvc: Card CVC code
                - zip_code: Random Japanese zip code
                - pref: Random Japanese prefecture
                - city: Random Japanese city
                - addr1: Random Japanese address line 1
                - addr2: Random Japanese address line 2
            Returns None if either resource is depleted.
        """
        async with self._lock:
            email_session = self._acquire_email_session_from_state_store()
            if email_session is None:
                return None
            card_data = self._acquire_card_for_session_from_state_store(
                email_session["session_id"]
            )
            if card_data is None:
                self._state_store.transition_session_resources(
                    email_session["session_id"],
                    {"email": ResourceState.RELEASED},
                )
                return None
            return {**email_session, **card_data}

    async def acquire_email_session(self) -> Optional[Dict[str, str]]:
        """Reserve only an email for a new workflow session."""
        async with self._lock:
            return self._acquire_email_session_from_state_store()

    async def acquire_card_for_session(
        self,
        session_id: str,
    ) -> Optional[Dict[str, str]]:
        """Reserve one unused card for an existing email-only session."""
        async with self._lock:
            return self._acquire_card_for_session_from_state_store(session_id)

    def _acquire_email_session_from_state_store(self) -> Optional[Dict[str, str]]:
        for email_line in self._stateful_email_lines():
            email_data = self._parse_email_line(email_line)
            if email_data is None:
                logger.error("Invalid email line (expected 4 ---- fields)")
                continue
            try:
                session_id = self._state_store.create_session_with_resources(
                    email_data["email"],
                    {"email": email_data["email"]},
                )
            except ResourceConflictError:
                continue
            return {
                "session_id": session_id,
                **email_data,
                **self._get_random_address(session_id),
            }
        return None

    def _acquire_card_for_session_from_state_store(
        self,
        session_id: str,
    ) -> Optional[Dict[str, str]]:
        if self._state_store.get_session(session_id) is None:
            return None
        for card_line in self._stateful_card_rows():
            card_data = self._parse_card_line(card_line)
            if card_data is None:
                logger.error("Invalid card line format")
                continue
            try:
                self._state_store.reserve_resources(
                    session_id,
                    {"card": card_data["card_no"]},
                )
            except ResourceConflictError:
                continue
            return card_data
        return None

    def _acquire_pair_from_state_store(self) -> Optional[Dict[str, str]]:
        """Reserve an email and card through SQLite without editing legacy files."""
        return None

    def load_email_for_session(self, session_id: str) -> Optional[Dict[str, str]]:
        """Reconstruct the email side of a session without requiring a card."""
        email_line = next(
            (
                line
                for line in self._source_email_lines()
                if self._state_store.session_owns_resource(
                    session_id,
                    "email",
                    line.split("----", 1)[0],
                )
            ),
            None,
        )
        if email_line is None:
            return None
        email_data = self._parse_email_line(email_line)
        if email_data is None:
            return None
        return {
            "session_id": session_id,
            **email_data,
            **self._get_random_address(session_id),
        }

    def load_card_for_session(self, session_id: str) -> Optional[Dict[str, str]]:
        """Reconstruct a card only when this session still owns its lease."""
        card_line = next(
            (
                row
                for row in self._source_card_rows()
                if self._state_store.session_owns_resource(
                    session_id,
                    "card",
                    self._card_identity(row),
                )
            ),
            None,
        )
        if card_line is None:
            return None
        card_data = self._parse_card_line(card_line)
        return card_data

    def load_pair_for_session(self, session_id: str) -> Optional[Dict[str, str]]:
        """Reconstruct a leased pair from input files for manual recovery."""
        email_data = self.load_email_for_session(session_id)
        card_data = self.load_card_for_session(session_id)
        if email_data is None or card_data is None:
            return None
        return {
            **email_data,
            **card_data,
        }

    # ═════════════════════════════════════════════════════════
    # Internal helpers
    # ═════════════════════════════════════════════════════════

    def _stateful_email_lines(self) -> List[str]:
        lines = self._source_email_lines()
        available = set(self._state_store.available_resource_values(
            "email",
            [line.split("----", 1)[0] for line in lines],
        ))
        return [line for line in lines if line.split("----", 1)[0] in available]

    def _stateful_card_rows(self) -> List[str]:
        rows = self._source_card_rows()
        available = set(self._state_store.available_resource_values(
            "card",
            [self._card_identity(row) for row in rows],
        ))
        return [row for row in rows if self._card_identity(row) in available]

    def _source_email_lines(self) -> List[str]:
        if not os.path.exists(self.email_file):
            return []
        with open(self.email_file, "r", encoding="utf-8") as fh:
            return [
                line.strip()
                for line in fh
                if line.strip() and "----" in line
            ]

    def _source_card_rows(self) -> List[str]:
        if not os.path.exists(self.cards_file):
            return []
        with open(self.cards_file, "r", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            try:
                next(reader)
            except StopIteration:
                return []
            return [
                ",".join(cell.strip() for cell in row)
                for row in reader
                if row
                and any(cell.strip() for cell in row)
                and row[-1].strip() != "已解綁"
            ]

    @staticmethod
    def _card_identity(row: str) -> str:
        parts = row.split(",")
        if len(parts) < 3:
            return ""
        offset = 1 if len(parts) >= 4 else 0
        return parts[offset]

    @staticmethod
    def _parse_email_line(line: str) -> Optional[Dict[str, str]]:
        parts = line.strip().split("----")
        if len(parts) < 4:
            return None
        return {
            "email": parts[0].strip(),
            "email_pwd": parts[1].strip(),
            "client_id": parts[2].strip(),
            "refresh_token": parts[3].strip().rstrip("$"),
        }

    @staticmethod
    def _parse_card_line(line: str) -> Optional[Dict[str, str]]:
        parts = line.strip().split(",")
        if len(parts) < 3:
            return None
        offset = 1 if len(parts) >= 4 else 0
        return {
            "card_no": parts[offset].strip(),
            "card_expiry": parts[offset + 1].strip(),
            "card_cvc": parts[offset + 2].strip(),
        }

    def _get_random_address(self, session_id: str = "") -> Dict[str, str]:
        """Get an address that is stable when reconstructing a session.

        Supports two formats:
          A) Standard: zip_code,pref,city,addr1,addr2
          B) Government data: 都道府県名,市区町村名,大字町丁目名,小字・通称名

        If zip_code is missing, generates a plausible Japanese postal code
        based on the prefecture name.

        Returns:
            Dict with keys: zip_code, pref, city, addr1, addr2
        """
        default_address = {
            "zip_code": "100-0001",
            "pref": "東京都",
            "city": "千代田区",
            "addr1": "千代田1-1-1",
            "addr2": "",
        }

        if not os.path.exists(self.address_file):
            logger.warning("Address file not found: %s", self.address_file)
            return default_address

        with open(self.address_file, "r", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        if not rows:
            logger.warning("Address file is empty: %s", self.address_file)
            return default_address

        generator = random
        if session_id:
            seed = int.from_bytes(
                hashlib.sha256(session_id.encode("utf-8")).digest()[:8],
                "big",
            )
            generator = random.Random(seed)

        row = generator.choice(rows)

        # ── Detect format ──────────────────────────────────────────────
        # Government format (実際の日本郵便局データ)
        if "都道府県名" in row:
            pref = row.get("都道府県名", "東京都").strip().strip('"')
            city = row.get("市区町村名", "").strip().strip('"')
            addr1 = row.get("大字町丁目名", "").strip().strip('"')
            addr2 = row.get("小字・通称名", "").strip().strip('"')
            zip_code = self._generate_zip_code(pref, generator)
        # Standard format (zip_code,pref,city,addr1,addr2)
        elif "zip_code" in row:
            pref = row.get("pref", "東京都")
            city = row.get("city", "")
            addr1 = row.get("addr1", "")
            addr2 = row.get("addr2", "")
            zip_code = row.get("zip_code") or self._generate_zip_code(
                pref,
                generator,
            )
        else:
            logger.warning("Unknown address_jp.csv format, using default")
            return default_address

        # ── Generate addr2 from buildings list + room number ──
        if not addr2.strip():
            addr2 = self._generate_address2(generator)

        return {
            "zip_code": zip_code,
            "pref": pref,
            "city": city,
            "addr1": addr1,
            "addr2": addr2,
        }

    def _generate_address2(
        self,
        generator: Optional[random.Random] = None,
    ) -> str:
        """Generate address2 = building name + room number."""
        rng = generator or random
        building = self._random_building(generator)
        floor = rng.randint(1, 12)
        room = rng.randint(1, 20)
        return f"{building} {floor}{room:02d}"

    def _random_building(
        self,
        generator: Optional[random.Random] = None,
    ) -> str:
        """Load a random building name from buildings_jp.csv."""
        bfile = os.path.join(self.input_dir, "buildings_jp.csv")
        if not os.path.exists(bfile):
            return "桜マンション"
        # Cache buildings list
        if not hasattr(self, "_building_cache"):
            with open(bfile, "r", encoding="utf-8") as fh:
                self._building_cache = [row[0] for row in csv.reader(fh) if row and row[0] and row[0] != "building_name"]
            logger.debug("Loaded %d building names", len(self._building_cache))
        return (generator or random).choice(self._building_cache)

    @staticmethod
    def _generate_zip_code(
        pref: str,
        generator: Optional[random.Random] = None,
    ) -> str:
        """Generate a plausible Japanese zip code from prefecture name.

        Uses a hardcoded pref→prefix mapping for major prefectures.
        Falls back to a random Tokyo-area code.

        Args:
            pref: Prefecture name (e.g. "東京都", "大阪府").

        Returns:
            7-char zip code string (e.g. "100-0001").
        """
        # Prefecture → zip code prefix (first 3 digits)
        PREF_ZIP = {
            "北海道": "060",    # 札幌市
            "青森県": "030",
            "岩手県": "020",
            "宮城県": "980",
            "秋田県": "010",
            "山形県": "990",
            "福島県": "960",
            "茨城県": "310",
            "栃木県": "320",
            "群馬県": "370",
            "埼玉県": "330",
            "千葉県": "260",
            "神奈川県": "220",
            "東京都": "100",
            "新潟県": "950",
            "富山県": "930",
            "石川県": "920",
            "福井県": "910",
            "山梨県": "400",
            "長野県": "380",
            "岐阜県": "500",
            "静岡県": "420",
            "愛知県": "450",
            "三重県": "510",
            "滋賀県": "520",
            "京都府": "600",
            "大阪府": "530",
            "兵庫県": "650",
            "奈良県": "630",
            "和歌山県": "640",
            "鳥取県": "680",
            "島根県": "690",
            "岡山県": "700",
            "広島県": "730",
            "山口県": "750",
            "徳島県": "770",
            "香川県": "760",
            "愛媛県": "790",
            "高知県": "780",
            "福岡県": "810",
            "佐賀県": "840",
            "長崎県": "850",
            "熊本県": "860",
            "大分県": "870",
            "宮崎県": "880",
            "鹿児島県": "890",
            "沖縄県": "900",
        }
        prefix = PREF_ZIP.get(pref, "100")
        suffix = (generator or random).randint(1000, 9999)
        return f"{prefix}-{suffix:04d}"

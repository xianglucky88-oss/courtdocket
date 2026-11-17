
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "courtdocket.db"

SPEC = SimpleNamespace(
    slug="courtdocket",
    fmt="html",
    engine="rpa",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts", "court"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
)

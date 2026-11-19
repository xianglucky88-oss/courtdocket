"""Regression tests for hearing-event interpretation (庭期结果).

Covers the scenarios from the docket requirements:

* 乱序通知 — an earlier-issued hearing notice arriving after a postponement
  must not move the schedule backwards;
* 重报 — duplicate reports and re-runs are idempotent;
* 跨页中断 / 恢复执行 — an interrupted pagination stores nothing partial and
  a resumed run converges to the same 庭期 as an uninterrupted one;
* 完整采集 / 续跑 / 历史恢复 — full, incremental and as-of views never
  produce contradictory hearing dates;
* 门户暂时封锁或缺页 — absence is never interpreted as 撤诉;
* 已有命令和未变化数据 — existing commands and legacy rows keep working.
"""
import json
import sqlite3

import pytest

from courtdocket import cli, framework, kernel
from courtdocket.store import Store


def notice(rid, status, ts, **extra):
    d = {"id": rid, "name": "Case %s" % rid, "value": 1,
         "status": status, "ts": ts, "court": "court_1"}
    d.update(extra)
    return d


@pytest.fixture(autouse=True)
def _clean_mock_state():
    from mockapp import main as mock
    mock._extra.clear()
    mock._verified_sessions.clear()
    mock._accepted_tokens.clear()
    mock.app.__dict__.pop("_flaky_n", None)
    yield
    mock._extra.clear()


@pytest.fixture
def store():
    con = sqlite3.connect(":memory:")
    st = Store(con)
    yield st
    con.close()


# --------------------------------------------------------------------------- #
# Event classification: 延期 / 改期 / 撤诉 / 未知输入 / 无效通知
# --------------------------------------------------------------------------- #
def test_process_classifies_each_event_type():
    assert kernel.process(notice("C", "postponed", "t"))["event"] == "postponed"
    assert kernel.process(notice("C", "rescheduled", "t"))["event"] == "rescheduled"
    assert kernel.process(notice("C", "withdrawn", "t"))["event"] == "withdrawn"
    ok = kernel.process(notice("C", "active", "t"))
    assert ok["event"] == "scheduled"
    assert ok["valid_notice"] == 1 and ok["invalid_reason"] == ""


def test_process_flags_unknown_and_invalid_notices():
    unk = kernel.process(notice("C", "whatever", "t"))
    assert unk["event"] == "unknown" and unk["invalid_status"] == 1
    assert unk["valid_notice"] == 0 and "unknown_status" in unk["invalid_reason"]
    no_ts = kernel.process(notice("C", "active", ""))
    assert no_ts["valid_notice"] == 0 and "missing_ts" in no_ts["invalid_reason"]
    no_id = kernel.process(notice("", "active", "t"))
    assert no_id["valid_notice"] == 0 and "missing_id" in no_id["invalid_reason"]


def test_reduce_does_not_mutate_records():
    recs = [notice("A", "active", "t1"), notice("A", "postponed", "t2")]
    out = kernel.reduce(recs)
    assert out == recs
    assert "regression" not in recs[0]


# --------------------------------------------------------------------------- #
# 乱序通知：延期后又推来更早发出的开庭通知，排期不倒退
# --------------------------------------------------------------------------- #
def test_out_of_order_earlier_notice_does_not_regress():
    notices = [
        notice("C1", "active", "2030-01-10T00:00:00Z"),     # 开庭通知
        notice("C1", "postponed", "2030-01-20T00:00:00Z"),  # 延期
        notice("C1", "active", "2030-01-05T00:00:00Z"),     # 更早发出、更晚到达
    ]
    today = kernel.schedule(notices)
    case = today["cases"]["C1"]
    assert case["event"] == "postponed"
    assert case["ts"] == "2030-01-20T00:00:00Z"
    # 可追查：被挡下的通知留在历史里
    assert case["counts"]["superseded"] == 2
    assert {h["effect"] for h in case["history"]} == {"effective", "superseded"}
    # 到达顺序不影响结果
    assert kernel.schedule(list(reversed(notices))) == today


def test_today_and_historical_views_are_each_correct():
    notices = [
        notice("C1", "active", "2030-01-10T00:00:00Z"),
        notice("C1", "postponed", "2030-01-20T00:00:00Z"),
        notice("C1", "active", "2030-01-05T00:00:00Z"),
    ]
    day11 = kernel.schedule(notices, as_of="2030-01-11T00:00:00Z")["cases"]["C1"]
    assert day11["event"] == "scheduled"
    assert day11["ts"] == "2030-01-10T00:00:00Z"
    day6 = kernel.schedule(notices, as_of="2030-01-06T00:00:00Z")["cases"]["C1"]
    assert day6["ts"] == "2030-01-05T00:00:00Z"
    before = kernel.schedule(notices, as_of="2030-01-01T00:00:00Z")
    assert "C1" not in before["cases"]


def test_postponement_then_later_notice_moves_forward_not_backward():
    notices = [
        notice("C1", "active", "2030-01-10T00:00:00Z"),
        notice("C1", "postponed", "2030-01-20T00:00:00Z"),
        notice("C1", "rescheduled", "2030-01-25T00:00:00Z"),   # 改期
    ]
    case = kernel.schedule(notices)["cases"]["C1"]
    assert case["event"] == "rescheduled"
    # 改期之后发出的开庭通知是新的庭期，不是倒退
    notices.append(notice("C1", "active", "2030-01-28T00:00:00Z"))
    case = kernel.schedule(notices)["cases"]["C1"]
    assert case["event"] == "scheduled"
    assert case["ts"] == "2030-01-28T00:00:00Z"


def test_withdrawal_is_terminal_and_traceable():
    notices = [
        notice("C1", "active", "2030-01-10T00:00:00Z"),
        notice("C1", "withdrawn", "2030-01-20T00:00:00Z"),  # 撤诉
        notice("C1", "active", "2030-01-25T00:00:00Z"),     # 撤诉后的乱通知
    ]
    case = kernel.schedule(notices)["cases"]["C1"]
    assert case["event"] == "withdrawn"
    assert case["state"] == "closed"
    assert case["counts"]["regressed"] == 1
    held = [h for h in case["history"] if h["effect"] == "regressed"]
    assert held and held[0]["ts"] == "2030-01-25T00:00:00Z"


def test_unknown_and_invalid_notices_have_explicit_consequences(store):
    store.upsert(notice("C9", "mystery", "2030-01-02T00:00:00Z"))
    store.upsert(notice("C9", "active", ""))                        # 缺 ts → 无效通知
    store.upsert(notice("", "active", "2030-01-02T00:00:00Z"))      # 缺 id
    sch = store.schedule()
    c9 = sch["cases"]["C9"]
    assert c9["event"] == "unknown" and c9["state"] == "unknown"
    assert c9["counts"]["invalid"] == 1
    assert len(sch["unattributed"]) == 1
    # 未知输入不会改写已知排期
    store.upsert(notice("C9", "active", "2030-01-03T00:00:00Z"))
    store.upsert(notice("C9", "mystery-2", "2030-01-04T00:00:00Z"))
    c9 = store.schedule()["cases"]["C9"]
    assert c9["event"] == "scheduled"
    assert c9["counts"]["regressed"] >= 1


# --------------------------------------------------------------------------- #
# 重报：重复上报与重复运行都是幂等的
# --------------------------------------------------------------------------- #
def test_repeated_reports_and_reruns_are_idempotent(store):
    rec = notice("C1", "active", "2030-01-02T00:00:00Z")
    assert store.upsert(rec) is True
    assert store.upsert(dict(rec)) is False
    assert store.count() == 1
    first = framework.run(mode="full", con=store.con)
    assert first["duplicates"] == 1          # 源里自带一条重报
    snapshot = store.schedule()
    second = framework.run(mode="incremental", con=store.con)
    assert second["ingested"] == 0
    assert store.schedule() == snapshot


# --------------------------------------------------------------------------- #
# 门户暂时封锁 / 缺页：不是撤诉，也不留下半成品
# --------------------------------------------------------------------------- #
def test_blocked_portal_is_not_read_as_withdrawal(store, monkeypatch):
    framework.run(mode="full", con=store.con)
    before = store.schedule()
    assert before["cases"]["COU-0002"]["event"] == "scheduled"

    real = framework.Transport.request

    def blocked(self, method, path, params=None, json_body=None, headers=None):
        if path == "/items":
            return 503, {}, "temporarily blocked"
        return real(self, method, path, params=params,
                    json_body=json_body, headers=headers)

    monkeypatch.setattr(framework.Transport, "request", blocked)
    with pytest.raises(RuntimeError):
        framework.run(mode="full", con=store.con)
    monkeypatch.undo()
    after = store.schedule()
    assert after == before                                  # 封锁没有改变任何庭期
    assert after["cases"]["COU-0002"]["event"] == "scheduled"  # 更不是撤诉


def test_cross_page_interruption_then_resume_converges(store, monkeypatch):
    real = framework.Transport.request

    def flaky(self, method, path, params=None, json_body=None, headers=None):
        if path == "/items" and (params or {}).get("page") == 2:
            return 500, {}, "page lost"
        return real(self, method, path, params=params,
                    json_body=json_body, headers=headers)

    monkeypatch.setattr(framework.Transport, "request", flaky)
    with pytest.raises(RuntimeError):
        framework.run(mode="full", con=store.con)
    assert store.count() == 0          # 跨页中断不留半成品
    monkeypatch.undo()

    result = framework.run(mode="full", con=store.con)   # 恢复执行
    assert result["ingested"] == 9

    baseline_con = sqlite3.connect(":memory:")
    baseline = Store(baseline_con)
    try:
        framework.run(mode="full", con=baseline_con)
        assert store.schedule() == baseline.schedule()   # 续跑与完整采集一致
    finally:
        baseline_con.close()


def test_rpa_interruption_then_resume_converges(store, monkeypatch):
    real = framework.Rpa._query_page

    def boom(self, page):
        if page == 2:
            raise RuntimeError("page lost")
        return real(self, page)

    monkeypatch.setattr(framework.Rpa, "_query_page", boom)
    with pytest.raises(RuntimeError):
        framework.run_rpa(con=store.con)
    assert store.count() == 0
    monkeypatch.undo()
    out = framework.run_rpa(con=store.con)
    assert out["ingested"] == 9
    assert store.schedule()["count"] == 9


# --------------------------------------------------------------------------- #
# 完整采集 / 续跑 / 历史恢复：不得互相矛盾
# --------------------------------------------------------------------------- #
def test_full_and_incremental_collections_agree(store):
    other_con = sqlite3.connect(":memory:")
    other = Store(other_con)
    try:
        framework.run(mode="full", con=store.con)
        t = framework.Transport()
        t.request("GET", "/refresh")                       # 门户新增一条
        framework.run(mode="incremental", con=store.con)   # 续跑
        framework.run(mode="full", con=other_con)          # 完整采集
        assert store.count() == other.count()
        assert store.schedule() == other.schedule()
        for as_of in ("2030-01-02T00:00:00Z", "2030-01-04T00:00:00Z",
                      "2030-02-15T00:00:00Z"):
            assert store.schedule(as_of=as_of) == other.schedule(as_of=as_of)
    finally:
        other_con.close()


def test_incremental_run_keeps_late_earlier_notice(store):
    framework.run(mode="full", con=store.con)
    assert store.schedule()["cases"]["COU-0002"]["ts"] == "2030-01-03T00:00:00Z"
    from mockapp import main as mock
    # 案件已有排期后，门户补推一份更早发出的开庭通知（ts 早于水位线）
    mock._extra.append(notice("COU-0002", "active", "2030-01-01T00:00:00Z"))
    out = framework.run(mode="incremental", con=store.con)
    assert out["ingested"] == 1                            # 没有被水位线丢掉
    case = store.schedule()["cases"]["COU-0002"]
    assert case["ts"] == "2030-01-03T00:00:00Z"            # 排期没有倒退
    assert len(case["history"]) == 2                       # 但通知已留痕
    past = store.schedule(as_of="2030-01-02T00:00:00Z")["cases"]["COU-0002"]
    assert past["ts"] == "2030-01-01T00:00:00Z"            # 历史时点同样正确


# --------------------------------------------------------------------------- #
# 已有命令和未变化数据继续可用
# --------------------------------------------------------------------------- #
def test_legacy_annotated_rows_remain_usable():
    con = sqlite3.connect(":memory:")
    st = Store(con)
    legacy = {"id": "L1", "status": "active", "ts": "2030-01-02T00:00:00Z",
              "state_norm": "active", "invalid_status": 0, "regression": 0}
    old_hash = kernel.hashlib.sha256(
        json.dumps(legacy, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]
    con.execute("INSERT INTO records VALUES(?,?,?,?)",
                (old_hash, "L1", "2030-01-02T00:00:00Z",
                 json.dumps(legacy, ensure_ascii=False)))
    con.commit()
    Store(con)  # re-init triggers the migration
    raw = {"id": "L1", "status": "active", "ts": "2030-01-02T00:00:00Z"}
    assert st.get(Store.hash(raw)) is not None
    assert st.upsert(raw) is False      # 未变化数据不会重复入库
    assert st.count() == 1
    assert st.schedule()["cases"]["L1"]["event"] == "scheduled"
    con.close()


def test_cli_run_and_schedule(capsys):
    con = Store().connect()
    Store(con).reset()
    con.close()
    try:
        cli.main(["--full"])
        run_out = json.loads(capsys.readouterr().out)
        assert run_out["engine"] == "rpa"
        cli.main(["--incremental"])
        inc = json.loads(capsys.readouterr().out)
        assert inc["engine"] == "rpa" and inc["ingested"] == 0
        cli.main(["--schedule"])
        today = json.loads(capsys.readouterr().out)
        assert today["as_of"] is None and len(today["cases"]) == 9
        cli.main(["--schedule", "--as-of", "2030-01-02T00:00:00Z"])
        past = json.loads(capsys.readouterr().out)
        assert past["as_of"] == "2030-01-02T00:00:00Z"
        assert sorted(past["cases"]) == ["COU-0001", "COU-0005", "COU-0006"]
    finally:
        con = Store().connect()
        Store(con).reset()
        con.close()

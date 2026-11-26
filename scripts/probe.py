# -*- coding: utf-8 -*-
"""一次性探针：按「今日」口径把所有会话日志汇总一遍，供核对，不改任何线上逻辑。

口径（与状态条既有标准一致，只把时间窗换成"今天 00:00 之后"）：
  · 轮数：按 conversationRequestId 聚合，最后一条记录是 assistant 消息、带 usage、
          无 error，且该请求完成时间落在今天；被 resend-fork-notice 作废的不算
  · 子代理：subagents/agent-*.jsonl 里的请求不算"对话轮次"，但它的花费算真钱
  · 消耗：rawUsage.credit 逐条累加（中断、回退、压缩、取标题都算，钱确实花了）
  · Token：rawUsage.total_tokens 逐条累加
  · 缓存命中：sum(prompt_cache_hit_tokens) / sum(prompt_tokens)
"""
import json
import os
import sys
import time
import glob
import datetime as dt
import collections

ROOT = os.path.expanduser("~/.workbuddy/projects")


def parse_ts(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            s = v.replace("Z", "+00:00")
            return dt.datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0
    return 0.0


def day_start(off_days=0):
    now = dt.datetime.now()
    d = dt.datetime(now.year, now.month, now.day) - dt.timedelta(days=off_days)
    return d.timestamp()


def scan(path, t0, t1):
    """返回该文件在 [t0, t1) 窗口内的统计，以及请求级状态（轮数要用）。"""
    st = {"credit": 0.0, "tok": 0, "pin": 0, "hit": 0, "recs": 0}
    reqs = {}            # rid -> {"ok","err","t","sub_only","n"}
    killed = set()
    user2req = {}
    pending = None
    try:
        f = open(path, "rb")
    except OSError:
        return st, reqs, killed
    with f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                o = json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                continue
            typ = o.get("type")
            pd = o.get("providerData")
            if not isinstance(pd, dict):
                pd = {}

            if typ == "resend-fork-notice":
                eid = o.get("editedUserItemId")
                rid = user2req.get(eid) if isinstance(eid, str) else None
                if rid:
                    killed.add(rid)
                continue

            if typ == "message" and o.get("role") == "user":
                uid = o.get("id")
                if isinstance(uid, str):
                    pending = uid
                continue

            rid = pd.get("conversationRequestId")
            if not isinstance(rid, str) or not rid:
                continue
            if pending:
                user2req[pending] = rid
                pending = None

            ts = parse_ts(o.get("timestamp"))
            e = reqs.setdefault(rid, {"ok": False, "t": 0.0, "sub_only": True, "n": 0})
            e["n"] += 1
            if not pd.get("isSubAgent"):
                e["sub_only"] = False
            if ts and (not e["t"] or ts < e["t"]):
                e["t"] = ts            # 请求开始时间（同请求取最早）

            ru = pd.get("rawUsage")
            if isinstance(ru, dict):
                c = ru.get("credit")
                if isinstance(c, (int, float)) and not isinstance(c, bool) and t0 <= ts < t1:
                    st["credit"] += float(c)
                for key, name in (("total_tokens", "tok"), ("prompt_tokens", "pin"),
                                  ("prompt_cache_hit_tokens", "hit")):
                    v = ru.get(key)
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and t0 <= ts < t1:
                        st[name] += int(v)
                if t0 <= ts < t1:
                    st["recs"] += 1

            e["ok"] = (typ == "message" and o.get("role") == "assistant"
                       and isinstance(pd.get("usage"), dict) and not pd.get("error"))
            e["done_t"] = ts           # 最后一条记录的时间（完成时间用这个）
    return st, reqs, killed


def main():
    t0 = day_start(0)
    t1 = time.time()
    print("今天窗口：%s ~ %s（本地时间）"
          % (dt.datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S"),
             dt.datetime.fromtimestamp(t1).strftime("%H:%M:%S")))
    files = sorted(glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True))
    tot = {"credit": 0.0, "tok": 0, "pin": 0, "hit": 0, "recs": 0}
    rounds = 0
    rows = []
    killed_all = set()
    for p in files:
        sub = os.sep + "subagents" + os.sep in p
        st, reqs, killed = scan(p, t0, t1)
        killed_all |= killed
        n_ok = 0
        for rid, e in reqs.items():
            if rid in killed:
                continue
            if not e["ok"] or e.get("done_t", 0) < t0:
                continue
            if sub or e["sub_only"]:
                continue
            n_ok += 1
        rounds += n_ok
        for k in tot:
            tot[k] += st[k]
        if st["recs"] or n_ok:
            rows.append((os.path.basename(p), "子代理" if sub else "会话",
                         n_ok, st["credit"], st["tok"], st["pin"], st["hit"]))

    print("\n%-12s %-6s %6s %10s %14s %10s" % ("文件", "类型", "轮", "消耗分", "Token", "命中率"))
    for name, kind, n, cr, tk, pin, hit in sorted(rows, key=lambda r: -r[3]):
        rate = ("%.1f%%" % (hit / pin * 100)) if pin else "-"
        print("%-12s %-6s %6d %10.2f %14s %10s"
              % (name[:12], kind, n, cr, format(tk, ","), rate))
    print("-" * 64)
    rate = ("%.2f%%" % (tot["hit"] / tot["pin"] * 100)) if tot["pin"] else "-"
    print("%-12s %-6s %6d %10.2f %14s %10s"
          % ("合计", "", rounds, tot["credit"], format(tot["tok"], ","), rate))
    print("\n今日：对话%d轮 · 消耗%.2f分 · Token总计%s · 缓存命中%s"
          % (rounds, tot["credit"], format(tot["tok"], ","), rate))
    print("明细：rawUsage 记录 %d 条；prompt_tokens %s；命中 %s"
          % (tot["recs"], format(tot["pin"], ","), format(tot["hit"], ",")))


if __name__ == "__main__":
    main()

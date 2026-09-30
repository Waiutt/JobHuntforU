# -*- coding: utf-8 -*-
"""扫岗**前置筛**：把「候选公司名单」按台账 + 额度表预筛，只输出**未投递且额度未满**的公司。

为什么需要：2026-09-27 用户指令「投递过的公司不用扫了」——已投公司**连抓都不要抓**，
否则既浪费时间，又会把已投公司混进线索文件（当日 SHEIN 5 条就是这样混进来的）。

用法：
    python -X utf8 scripts/prefilter_companies.py <候选文件.md>              # 全量分类输出
    python -X utf8 scripts/prefilter_companies.py <候选文件.md> --fit        # 只列「契合度达标」的可扫公司
    python -X utf8 scripts/prefilter_companies.py <候选文件.md> --json

候选文件：Markdown 表格（自动按表头找「公司」列，支持一个文件多张表）；也支持纯文本每行一家。
口径：公司归一走 dashboard/_jobhunt_common.py（与 daily_scan.py / check_dupe.py 同一套别名表），
      已投 = 台账 status ∈ {Submitted, Assessment, ApplicationUpdated}；
      剔除 = site_quotas.json 的 quota_exhausted / no_matching_role 及 company_level_blocked。
"""
import csv
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard"))
sys.stdout.reconfigure(encoding="utf-8")

from _jobhunt_common import resolve_company, normalize  # noqa: E402

ROOT = r"D:\dsh\JobHuntBot-main"
LEDGER = os.path.join(ROOT, "dashboard", "application_log.csv")
QUOTA = os.path.join(ROOT, "dashboard", "site_quotas.json")
BLOCK_ST = {"quota_exhausted", "no_matching_role"}
NAME_COLS = ("公司", "公司名", "公司名称", "企业", "企业名称", "company")
# 契合度关键词：岗位方向 + 城市（用户口径：江浙沪 + 北京优先）
FIT_ROLE = ("运营", "内容", "数据", "产品", "市场", "用户", "增长", "策划", "品牌", "电商", "策略", "分析", "社区", "直播")
FIT_CITY = ("上海", "杭州", "南京", "北京", "苏州", "无锡", "宁波", "合肥", "全国", "多地")


def canon(name):
    r = resolve_company(name)
    c = r.get("canonical") if isinstance(r, dict) else r
    return c or name


def nkey(name):
    """统一比较键：canonical 再过一次 normalize（台账/额度/别名表写法不同，必须同口径）。"""
    return normalize(canon(name))


def load_applied():
    with io.open(LEDGER, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    applied, cnt = set(), {}
    for r in rows:
        if r["status"] in ("Submitted", "Assessment", "ApplicationUpdated"):
            k = nkey(r["company"])
            applied.add(k)
            cnt[k] = cnt.get(k, 0) + 1
    return applied, cnt


def load_blocked():
    d = json.load(io.open(QUOTA, encoding="utf-8"))
    blocked = {}
    for q in d["quotas"]:
        if q.get("status") in BLOCK_ST:
            blocked[normalize(q.get("canonical") or q["company"])] = "%s %s/%s" % (
                q.get("status"), q.get("used"), q.get("limit"))
    for b in d.get("company_level_blocked", []):
        blocked[normalize(b["canonical"])] = b.get("reason", "blocked")
    return blocked


def extract(path):
    """→ [(公司, 该行其余文本)]；支持一个文件里多张表。"""
    s = io.open(path, encoding="utf-8-sig").read()
    out, col = [], None
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 2 or re.match(r"^[-: ]+$", cells[0]):
            continue
        is_hdr = any(h.replace("*", "").strip() in NAME_COLS for h in cells)
        if is_hdr:
            col = next(i for i, h in enumerate(cells) if h.replace("*", "").strip() in NAME_COLS)
            continue
        if col is None:                       # 无表头 → 退化：跳过纯序号列
            col = 1 if re.fullmatch(r"\d+", cells[0]) else 0
        if col >= len(cells):
            continue
        v = re.sub(r"[*`]", "", cells[col]).strip()
        if not v or re.fullmatch(r"\d+", v):
            continue
        rest = " ".join(c for i, c in enumerate(cells) if i != col)
        out.append((v, rest))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    rows = extract(args[0])
    seen, uniq = set(), []
    for c, rest in rows:
        if c in seen:
            continue
        seen.add(c)
        uniq.append((c, rest))
    applied, cnt = load_applied()
    blocked = load_blocked()
    keep, drop_applied, drop_blocked = [], [], []
    for c, rest in uniq:
        k = nkey(c)
        if k in blocked:
            drop_blocked.append((c, blocked[k]))
        elif k in applied or any(len(k) >= 3 and k in a for a in applied) or any(len(a) >= 3 and a in k for a in applied):
            # 🔴 2026-09-27 补：别名表未桥接「长短名」时的兜底（例：台账「作业帮教育科技（北京）有限公司」vs 清单「作业帮」）
            drop_applied.append((c, cnt.get(k, 0)))
        else:
            fit = any(w in rest for w in FIT_ROLE) and any(w in rest for w in FIT_CITY)
            keep.append((c, k, fit, rest))
    print("候选 %d 家 → 未投 %d（其中契合 %d）｜ 已投剔除 %d ｜ 额度/黑名单剔除 %d"
          % (len(uniq), len(keep), sum(1 for x in keep if x[2]), len(drop_applied), len(drop_blocked)))
    show = [x for x in keep if x[2]] if "--fit" in sys.argv else keep
    print("\n── ✅ 可扫（未投递%s） ──" % ("·契合度达标" if "--fit" in sys.argv else ""))
    for c, k, fit, rest in show:
        tag = "⭐" if fit else "  "
        print("  %s %-32s | %s" % (tag, c[:32], rest.replace("\n", " ")[:88]))
    if "--fit" not in sys.argv:
        print("\n（加 --fit 只看契合度达标的）")
    print("\n── ⛔ 已投（用户规则：不扫） ──")
    for c, n in sorted(drop_applied, key=lambda x: -x[1]):
        print("   %-32s 已投 %d 笔" % (c[:32], n))
    print("\n── ⛔ 额度满/黑名单 ──")
    for c, why in drop_blocked:
        print("   %-32s %s" % (c[:32], why))
    if "--json" in sys.argv:
        print("\n" + json.dumps({"keep": [c for c, _, _, _ in keep],
                                 "keep_fit": [c for c, _, f, _ in keep if f],
                                 "applied": [c for c, _ in drop_applied],
                                 "blocked": [c for c, _ in drop_blocked]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

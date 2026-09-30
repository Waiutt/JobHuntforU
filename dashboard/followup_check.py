# -*- coding: utf-8 -*-
"""
followup_check.py — 时效跟进与截止提醒（每日开工第一件事）

背景（本工具存在的理由，写在 `待办与截止提醒.md` 开头）:
    「2026-09-12 发现米哈游笔试、三七互娱笔试、汉得在线测评三项全部因无提醒而超时作废。
      教训：没有截止日期的提醒 = 没有提醒。」

现状问题：截止信息以**散文表格**散落在 350 行的 `待办与截止提醒.md` 里，
          而结构化载体 `follow_up.csv`（含 deadline / next_action / status 列）**几乎是空的**。
          → 机器读不到，人和 Agent 都得靠通读，必然漏。

本工具做三件事:
    ① scan   从 `待办与截止提醒.md` 抽取时效行，解析截止日期与剩余天数
    ② 输出   按时限排序的清单：🔴 逾期 / 🔥 72h 内 / 🟡 7 天内 / ⚪ 远期 / ❓ 无法解析
    ③ export 把可解析项写回 `follow_up.csv`（结构化），供后续机器消费

用法:
    python -X utf8 dashboard/followup_check.py                 # 只看 72h 内 + 逾期
    python -X utf8 dashboard/followup_check.py --all           # 全部
    python -X utf8 dashboard/followup_check.py --export        # 写回 follow_up.csv
    python -X utf8 dashboard/followup_check.py --date 2026-09-25   # 指定"今天"（测试用）
"""
import csv
import os
import re
import sys
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, '待办与截止提醒.md')
CSV_OUT = os.path.join(BASE, 'follow_up.csv')

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 已作废/已完成/已出结果的行不再报警
DONE_PAT = re.compile(r'已过期|已完成|已用满|已结束|作废|不可投|已收感谢信|流程终止|已跳过|'
                      r'已投递[（(]|已投递。|已投递\s|已核|已提交|'
                      r'未通过|初筛未过|暂不匹配|未匹配|感谢信')
# ⚠️ 注意：不能用裸「已投递」「已提交」——
#    「表单已填好待提交」既含「已投递」（子串）又含「提交」，
#    误判会把真正的待提交行动项整批过滤掉（本工具第一版就踩了）。
# 这类"截止"不是真日期，静默跳过
NON_DATE = re.compile(r'^(—|-+|–|待释放|待岗位开放|尽快|待确认|待定|无|待材料|长期|未提供)$')

DATE_PATS = [
    (re.compile(r'(\d{4})-(\d{2})-(\d{2})'), 'ymd'),
    (re.compile(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日'), 'md'),
    # 只认带分隔符的 M-D / M/D。
    # 🔴 不能写成 (\d{1,2})[-/]?(\d{1,2}) —— 会把「3志愿」「2个」当成 3月X日/2月X日，
    #    进而伪造出错误的"截止日期"（第一版踩过：把"3志愿"解析成 2026-03-01）。
    (re.compile(r'(?<!\d)(\d{1,2})\s*[-/]\s*(\d{1,2})(?!\d)'), 'md'),
]


def parse_deadline(cell, ref_year, ref_today=None):
    """
    从单元格文本抽出截止日期。返回 (date|None, 原文片段)

    ref_today: 无年份的日期用它兜底判定合理性（防「9.9千」这类薪资文本被当成 09-09）
    """
    cell = (cell or '').strip()
    if not cell or NON_DATE.match(cell):
        return None, cell

    # ⚠️ 区间写法「网申窗口 08-22~09-19」「9/1-9/30」：**截止 = 区间右端**，不是左端。
    #    第一版取了左端 → 把"窗口开启日"当成"截止日"（哈啰那行曾解析成 08-22）。
    mrange = re.search(r'(?<![\d.])(\d{4}-\d{2}-\d{2}|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*[-/]\s*\d{1,2})'
                       r'\s*[~～至到]{1,2}\s*'
                       r'(\d{4}-\d{2}-\d{2}|\d{1,2}\s*月\s*\d{1,2}\s*日|\d{1,2}\s*[-/]\s*\d{1,2})', cell)
    if mrange:
        d2, frag2 = parse_deadline(mrange.group(2), ref_year, ref_today)
        if d2:
            return d2, frag2

    m = DATE_PATS[0][0].search(cell)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(0)
        except ValueError:
            return None, m.group(0)
    for pat, kind in DATE_PATS[1:]:
        m = pat.search(cell)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                continue
            try:
                cand = date(ref_year, mo, d)
            except ValueError:
                continue
            # 🔴 年龄守卫：无年份的 M-D 若已明显过期（>30 天），更可能是薪资/数量文本
            #    （如「9.9-9.9千·杭州」→ 误读为 09-09），宁可判为「无日期」也不误报逾期。
            if ref_today and (ref_today - cand).days > 30:
                continue
            return cand, m.group(0)
    return None, cell


def iter_md_tables(path):
    """逐行扫描 markdown 表格，产出 (行号, 表头list, 正文list, 上文最近标题)"""
    with open(path, encoding='utf-8') as f:
        lines = f.read().splitlines()
    heading = ''
    hdr, buf, start = None, [], 0
    for i, ln in enumerate(lines, 1):
        if ln.startswith('#'):
            heading = ln.lstrip('#').strip()
        is_row = ln.strip().startswith('|') and ln.count('|') >= 2
        if is_row:
            cells = [c.strip() for c in ln.strip().strip('|').split('|')]
            if hdr is None:
                hdr, buf, start = cells, [], i
                continue
            if all(re.fullmatch(r':?-{2,}:?', c or '-') for c in cells):
                continue
            buf.append((i, cells))
        else:
            if hdr is not None and buf:
                yield start, hdr, buf, heading
            hdr, buf = None, []
    if hdr is not None and buf:
        yield start, hdr, buf, heading


COL_HINTS = {
    'company': ['公司', '企业', '单位', '机构'],
    'event':   ['事项', '事件', '内容', '事由', '项目'],
    'deadline': ['截止', '期限', '时间', '日期'],
    'status':  ['状态', '结果', '剩余', '进度', '说明'],
}


def build_colmap(hdr):
    """
    按表头形状决定列映射。
    ⚠️ 待办表里有两种表形，必须分开处理（第一版按固定列名找，导致「时效类」表
       的 公司列 全部为空、截止日期被误当公司名）：
         A. 进行中/待办 表 ：| 公司 | 事项 | 截止 | 剩余 | 状态 |
         B. 时效/倒计时 表 ：| 优先级 | 事项 | 截止 | 剩余 |
    """
    m = {}
    for kind, hints in COL_HINTS.items():
        for h in hints:
            for i, c in enumerate(hdr):
                if h in c and kind not in m:
                    m[kind] = i
    # 形状 B：没有公司列 → 用「事项」同时充当公司/事项
    if 'company' not in m and 'event' in m:
        m['company'] = None
    # 状态列若未命中，用「剩余」兜底
    if 'status' not in m:
        for i, c in enumerate(hdr):
            if '剩余' in c or '备注' in c or '说明' in c:
                m['status'] = i
                break
    return m


def find_col(hdr, kind):
    for h in COL_HINTS[kind]:
        for i, c in enumerate(hdr):
            if h in c:
                return i
    return None


def collect(today):
    items = []
    for lineno, hdr, rows, heading in iter_md_tables(SRC):
        cm = build_colmap(hdr)
        if 'event' not in cm and 'company' not in cm:
            continue
        for ln, cells in rows:
            def g(i):
                return cells[i] if (i is not None and i < len(cells)) else ''
            company, event = g(cm.get('company')), g(cm.get('event'))
            dl_raw, st = g(cm.get('deadline')), g(cm.get('status'))
            blob = ' '.join(cells)
            kind = 'done' if DONE_PAT.search(blob) else 'open'
            dd, frag = parse_deadline(dl_raw, today.year, today)
            if dd is None and kind == 'open':
                # 截止列没日期时，再全行找一次（很多行把日期写在"剩余"或"状态"里）
                for c in cells:
                    dd, frag = parse_deadline(c, today.year, today)
                    if dd:
                        break
            # 形状 B：事项列同时含公司名，拆出「公司 · 岗位」前缀
            if not company and event:
                part = re.split(r'[·|｜:：]', event, 1)
                if len(part) == 2 and len(part[0]) <= 24:
                    company, event = part[0], part[1]
            days = (dd - today).days if dd else None
            items.append({
                'line': ln, 'section': heading, 'company': company, 'event': event,
                'deadline_raw': dl_raw, 'deadline': dd.isoformat() if dd else '',
                'days_left': days, 'status_raw': st, 'kind': kind,
            })
    return items


def normalize_key(s, keep=None):
    """去 markdown 标记/标点/空白，便于归并同一事项的不同写法"""
    s = re.sub(r'[*~`\[\]()（）【】<>《》「」]', '', (s or ''))
    s = re.sub(r'[，,。.、；;：:！!？?\-—_/\\|]', '', s)
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'^(❓|🔴+|🟡|🟢|⏹️|✅|⚠️)+', '', s)
    # 统一简称/全称，便于归并（龙旗科技 有限公司 == 龙旗科技）
    s = s.replace('有限公司', '').replace('股份', '').replace('集团', '')
    s = s.replace('南京邮电大学', '南邮').replace('南邮专场宣讲', '南邮宣讲')
    return s[:keep] if keep else s


def key_of(it):
    """
    归并键 = 公司核心词 + 事项核心词。
    ⚠️ 难点：同一事项在不同小节里，「公司」可能写在 公司列 也可能写在 事项列
        （例：「腾讯」/「综合素质测评」 vs 「」/「腾讯综合素质测评」）。
        → 因此把两者**拼接**后再取核心词，两种写法就能落到同一键。
    """
    a = normalize_key(it['company'])
    b = normalize_key(it['event'])
    return (a + b)[:8]


def dedupe(items):
    """
    同一事项会在多个「日期追加」小节里重复登记（腾讯测评出现 5 次、字节面试 6 次）。
    按「公司+事项」归并：
      · 保留**最详细**的一条（status 最长）
      · 截止日期取**最早**的非空值（越早越保守，宁可早报）
      · 任一处置为「未结」→ 合并项视为未结
    """
    merged = {}
    for it in items:
        key = key_of(it)
        if key not in merged:
            it['occurrences'] = [it['line']]
            merged[key] = it
            continue
        cur = merged[key]
        cur['occurrences'].append(it['line'])
        if len(it['status_raw']) > len(cur['status_raw']):
            cur['status_raw'] = it['status_raw']
        if not cur['company'] and it['company']:
            cur['company'] = it['company']
        if not cur['event'] and it['event']:
            cur['event'] = it['event']
        if it['deadline'] and (not cur['deadline'] or it['deadline'] < cur['deadline']):
            cur['deadline'] = it['deadline']
            cur['days_left'] = it['days_left']
            cur['deadline_raw'] = it['deadline_raw']
        if it['kind'] == 'open':
            cur['kind'] = 'open'
    return list(merged.values())


def bucket(it):
    if it['kind'] == 'done':
        return 'done'
    d = it['days_left']
    if d is None:
        return 'nodate'
    if d < 0:
        return 'overdue'
    if d <= 3:
        return 'urgent'
    if d <= 7:
        return 'week'
    return 'later'


BADGE = {'overdue': '🔴 逾期', 'urgent': '🔥 72h内', 'week': '🟡 7天内',
         'later': '⚪ 远期', 'nodate': '❓ 无日期', 'done': '✅ 已结'}


def clean_md(s):
    """去掉 markdown 标记，导出 CSV 用"""
    s = re.sub(r'~~|\*\*|`', '', s or '')
    s = re.sub(r'^[#>\s]+', '', s)
    return s.strip()


def export_csv(items):
    """
    把可解析截止日期的**未结**项写回 follow_up.csv（结构化，供机器消费）。
    注意：**跳过已过期** —— 已过期项写进去只会让后续流程反复报警，
    它们的处置是「确认失败并归档」，不是「待办」。
    """
    open_items = [i for i in items
                  if i['kind'] == 'open' and i['deadline']
                  and (i['days_left'] is None or i['days_left'] >= 0)]
    with open(CSV_OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['date', 'company', 'job_title', 'contact', 'channel',
                    'event_type', 'deadline', 'next_action', 'status', 'notes', 'time'])
        for it in sorted(open_items, key=lambda x: x['deadline']):
            left = it['days_left']
            w.writerow([
                date.today().isoformat(),
                clean_md(it['company']),
                clean_md(it['event'])[:80],
                '', '',
                clean_md(it['section'])[:40],
                it['deadline'],
                clean_md(it['status_raw'])[:120] or '见待办与截止提醒.md',
                'pending',
                '来源: 待办与截止提醒.md:%s' % ','.join(
                    str(x) for x in it.get('occurrences', [it['line']])),
                ('剩 %d 天' % left) if left is not None else '',
            ])
    return len(open_items)


def scan_pending_from_ledger():
    """
    「待提交 / 等用户定夺」的结构化来源 = 台账 status ∈ {NeedsUser, ToApply}。

    ⚠️ 为什么不用正则扫进度文件：那些文件把状态写在散文里，
       用 regex 抽取必然要么漏、要么把「必须点预览并提交」这类方法论句子当成行动项
       （本工具第一版就踩了，噪音淹没信号）。台账是有明确 status 列的**结构化**来源，可靠得多。
    """
    import csv
    path = os.path.join(BASE, 'application_log.csv')
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            st = (r.get('status') or '').strip()
            if st in ('NeedsUser', 'ToApply'):
                out.append({
                    'status': st,
                    'date': r.get('attempt_date', ''),
                    'company': r.get('company', ''),
                    'job': (r.get('job_title') or '')[:56],
                    'note': (r.get('notes') or '')[:90],
                })
    return out


def main():
    argv = sys.argv[1:]
    today = date.today()
    if '--date' in argv:
        i = argv.index('--date')
        today = datetime.strptime(argv[i + 1], '%Y-%m-%d').date()
    show_all = '--all' in argv
    do_export = '--export' in argv

    if not os.path.exists(SRC):
        print('❌ 找不到 %s' % SRC)
        return 1

    items = dedupe(collect(today))
    groups = {}
    for it in items:
        groups.setdefault(bucket(it), []).append(it)

    print('=' * 78)
    print('时效跟进检查 · 今天 %s' % today.isoformat())
    print('来源：%s  （去重后 %d 项）' % (os.path.basename(SRC), len(items)))
    print('=' * 78)
    order = ['overdue', 'urgent', 'week', 'later', 'nodate', 'done']
    print('  统计：' + ' ｜ '.join(
        '%s %d' % (BADGE[k], len(groups.get(k, []))) for k in order if groups.get(k)))
    print()

    for k in (['overdue', 'urgent', 'week', 'later', 'nodate'] if show_all else ['overdue', 'urgent']):
        rows = groups.get(k) or []
        if not rows:
            continue
        rows.sort(key=lambda x: (x['days_left'] is None, x['days_left'] or 0))
        print('── %s（%d）──' % (BADGE[k], len(rows)))
        for it in rows:
            dl = it['deadline'] or it['deadline_raw'] or '—'
            left = '' if it['days_left'] is None else (
                '还剩 %d 天' % it['days_left'] if it['days_left'] >= 0
                else '已逾期 %d 天' % -it['days_left'])
            occ = len(it.get('occurrences', [1]))
            print('  %-12s %-12s %s%s' % (dl[:12], left, (it['company'] or '')[:26],
                                          ('  ×%d 处登记' % occ) if occ > 1 else ''))
            print('      %s' % (it['event'] or '')[:96])
            print('      → %s  〔待办表:%s〕' % (
                (it['status_raw'] or '')[:70],
                ','.join(str(x) for x in it.get('occurrences', [it['line']])[:6])))
        print()

    if not show_all and not groups.get('overdue') and not groups.get('urgent'):
        print('🎉 未来 72 小时内没有到期项。')
        print('   看全部请加 --all')

    # ── 待提交 / 等用户定夺（结构化解法：台账 status=NeedsUser/ToApply）──
    pending = scan_pending_from_ledger()
    if pending:
        print('─' * 78)
        print('⏸️  待提交 / 等用户定夺（%d）—— 台账 status ∈ {NeedsUser, ToApply}' % len(pending))
        print('     这类没有截止日期，不会被到期提醒捞起来；表单可能已填到最后一格。')
        for p in pending:
            print('   · [%s] %s ／ %s' % (p['status'], p['company'][:30], p['job']))
            print('       %s  〔台账 %s〕' % (p['note'][:80], p['date']))
        print()

    if do_export:
        n = export_csv(items)
        print('📤 已写回 %s（%d 条带日期的未结项）' % (os.path.basename(CSV_OUT), n))

    # 无日期的未结项 = 真正的隐患（"没有截止日期的提醒 = 没有提醒"）
    nod = groups.get('nodate') or []
    if nod:
        print()
        print('⚠️ 有 %d 条**未结但没有可解析截止日期** —— 这才是最容易再翻车的一类：' % len(nod))
        for it in nod[:12]:
            print('   · %s ／ %s  〔待办表:%d〕' % (
                (it['company'] or '')[:24], (it['event'] or '')[:56], it['line']))
        if len(nod) > 12:
            print('   … 其余 %d 条见 --all' % (len(nod) - 12))
        print('   ➡️ 建议逐条补上绝对日期，并登记进 follow_up.csv')
    return 0


if __name__ == '__main__':
    sys.exit(main())

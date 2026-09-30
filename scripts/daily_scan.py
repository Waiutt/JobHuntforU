# -*- coding: utf-8 -*-
"""
daily_scan.py — 每日开工扫岗（方案第 4.1 节 8 步流程）

用法:
    # 1) 从线索文件扫（CSV / Markdown 表格 / 管道分隔文本均可）
    python -X utf8 scripts/daily_scan.py dashboard/牛客推送候选池.md

    # 2) 扫台账里的候选池（job_pool.csv）
    python -X utf8 scripts/daily_scan.py --pool

    # 3) 直接给几行
    python -X utf8 scripts/daily_scan.py --inline "公司|岗位|城市|薪资|入口" ...

    # 4) 重建简历索引
    python -X utf8 scripts/daily_scan.py --resumes

    # 常用开关
    --out dashboard/每日扫岗_2026-09-23.md   指定输出文件（默认按当天日期命名）
    --limit 50                               最多处理多少条线索
    --json                                   额外输出 JSON 便于程序消费

流程（严格按第 4.1 节）:
  ① 拉线索 → ② 公司名过 company_aliases 归一化 → ③ 公司级去重 → ④ 岗位级去重
  → ⑤ 门槛预检（学历/GPA/届别/专业/英语/经验）→ ⑥ 额度核对
  → ⑦ 输出《每日扫岗_YYYY-MM-DD.md》可投清单
  ⑧ 纪律：扫岗表未出，不许开填表单

列名自动识别（中英/多种写法），至少要有「公司」列；「岗位」列可选。
"""
import csv
import io
import json
import os
import re
import sys
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'dashboard'))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from _jobhunt_common import (  # noqa: E402
    verdict, resolve_company, normalize, normalize_job, load_pool,
    quota_for, alias_config, POSITION_LEVEL_STATUS, COMPANY_LEVEL_STATUS,
)

PROFILE = os.path.join(PROJECT_ROOT, 'candidate_profile.json')

# ── 列名识别 ────────────────────────────────────────────────────────────
COL_MAP = {
    'company':  ['公司', '公司名', '企业', 'company', '雇主', '单位'],
    'job':      ['岗位', '岗位名', '职位', 'job', 'job_title', '职位名称', '岗位名称'],
    'city':     ['城市', '地点', '工作地点', 'city', 'location', '所在地', '位置'],
    'salary':   ['薪资', '薪酬', '工资', 'salary', '待遇', '年薪', '月薪'],
    'url':      ['入口', '链接', 'url', '投递入口', 'job_url', '地址'],
    'platform': ['平台', '渠道', 'platform', '来源', 'source', 'ats'],
    'jd':       ['jd', '岗位描述', '描述', '要求', 'jd原文', 'job_description', '门槛'],
    'status':   ['状态', 'status'],
    # 池子专有列（不加这些，city/platform 会全空）
    'cohort':   ['cohort_match_status', '届别匹配', '届别'],
    'level':    ['level', '层级', '招聘类型'],
    'stage':    ['current_stage', '阶段'],
    'priority': ['priority', '优先级'],
}

# ── 门槛预检规则（第 4.1 ⑤）────────────────────────────────────────────
# ⚠️ 只做「明确写在 JD 里」的硬门槛识别；识别不到一律标「未检出」，交人工读 JD。
GATE_RULES = [
    ('gpa',      r'(GPA|绩点|平均分)[^。；;\n]{0,12}?(\d\.\d+)\s*(?:/\s*(\d(?:\.\d+)?))?\s*(以上|及以上|不低于|\+)',
     'JD 要求 GPA ≥ {0}'),
    ('degree',   r'(硕士|研究生)(研究生)?(及以上|以上|学历)?', 'JD 要求硕士及以上'),
    ('degree_phd', r'(博士|PhD|Ph\.?D)', 'JD 要求博士'),
    ('major',    r'(仅限|限|要求)[^。；;\n]{0,10}(计算机|软件工程|数学|统计|市场营销|广告|新闻传播)[^。；;\n]{0,6}专业',
     'JD 限定了专业'),
    ('english_fluent', r'(英语|英文)[^。；;\n]{0,10}(流利|母语|工作语言|可作为工作语言|地道)', 'JD 要求英语流利/工作语言'),
    ('korean',   r'(韩国语|韩语)[^。；;\n]{0,6}(精通|流利|TOPIK)', 'JD 要求韩语精通'),
    ('lang_other', r'(日语|日文)[^。；;\n]{0,8}(N1|N2|精通|流利|母语)|(西班牙语|德语|法语|俄语)[^。；;\n]{0,8}(精通|流利|母语|B2|C1)',
     'JD 要求小语种（日/西/德/法/俄）→ 本人无 → 不匹配'),
    ('exp3',     r'(\d)\s*年(以上)?(相关)?(工作)?经验', 'JD 提到 {0} 年经验（校招岗需确认是否硬性）'),
    ('intern_only', r'(实习转正|仅实习|实习生岗位|日薪)', '疑似实习/日薪岗 → 按口径应排除'),
    ('sales_commission', r'(纯提成|底薪\+提成|地推)', '疑似销售提成岗 → 按口径应排除'),
]

# 本人的可对照事实（从 profile 读，读不到就跳过该项）
FALLBACKS = {
    'degree': '本科',
    'gpa': 2.66,
    'cohort': '2027届',
    'major': '数字媒体技术',
    'english': 'CET-6 428（写实，不写流利）',
}


def load_profile():
    try:
        with open(PROFILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def pick(row, kind):
    for k in COL_MAP.get(kind, []):
        for rk in row:
            if normalize(rk) == normalize(k):
                v = (row[rk] or '').strip()
                if v:
                    return v
    return ''


def _all_known_headers():
    out = set()
    for ks in COL_MAP.values():
        for k in ks:
            out.add(normalize(k))
    return out


KNOWN_HEADERS = _all_known_headers()


def _looks_like_header(cells):
    """首行是否像表头：至少 2 个单元格命中已知列名，且不含明显的数据特征"""
    hits = sum(1 for c in cells if normalize(c) in KNOWN_HEADERS)
    return hits >= 2


def sniff_table(text):
    """把线索文件解析为 list[dict]。支持 Markdown 表格 / CSV / 管道分隔行（可带表头）。"""
    lines = [l.rstrip('\n') for l in text.splitlines()]
    md_rows = [l for l in lines if l.strip().startswith('|') and l.count('|') >= 2]
    if len(md_rows) >= 2:
        cells = []
        for l in md_rows:
            parts = [p.strip() for p in l.strip().strip('|').split('|')]
            if all(re.fullmatch(r':?-{2,}:?', p or '-') for p in parts):
                continue  # 分隔行
            cells.append(parts)
        if cells:
            hdr = cells[0]
            return [dict(zip(hdr, r + [''] * (len(hdr) - len(r)))) for r in cells[1:]]
    # CSV
    if ',' in text and '\n' in text:
        try:
            rd = list(csv.DictReader(io.StringIO(text)))
            if rd and any(k for k in rd[0].keys() if k):
                return rd
        except Exception:
            pass
    # 管道分隔 / 制表符分隔的裸行（首行可能是表头，也可能直接是数据）
    raw = []
    for l in lines:
        if not l.strip() or l.strip().startswith('#'):
            continue
        sep = '|' if '|' in l else ('\t' if '\t' in l else None)
        if not sep:
            continue
        parts = [p.strip() for p in l.split(sep) if p.strip() != '']
        if len(parts) >= 2:
            raw.append(parts)
    if not raw:
        return []
    out = []
    if _looks_like_header(raw[0]):
        hdr = raw[0]
        for parts in raw[1:]:
            out.append(dict(zip(hdr, parts + [''] * (len(hdr) - len(parts)))))
    else:
        for parts in raw:
            out.append({'公司': parts[0], '岗位': parts[1],
                        '城市': parts[2] if len(parts) > 2 else '',
                        '薪资': parts[3] if len(parts) > 3 else '',
                        '入口': parts[4] if len(parts) > 4 else ''})
    return out


def gate_check(jd_text):
    """⑤ 门槛预检：返回 (verdict, [命中说明])"""
    if not jd_text:
        return '未检出', []
    hits = []
    for kind, pat, msg in GATE_RULES:
        m = re.search(pat, jd_text, re.I)
        if not m:
            continue
        if kind == 'gpa':
            req = float(m.group(2))
            scale = float(m.group(3)) if m.group(3) else 4.0
            mine = FALLBACKS['gpa']
            if scale >= 4:
                ok = mine >= req
                hits.append(('pass' if ok else 'fail',
                             'JD 要求 GPA ≥ %.2f/%s；本人 %.2f/4.0 → %s' % (req, m.group(3) or '4', mine, '达标' if ok else '不达标')))
            else:
                hits.append(('unknown', 'JD 要求 GPA ≥ %s/%s，分制与本人 2.66/4.0 不可直接比较 → 需确认' % (m.group(2), m.group(3))))
        elif kind == 'degree':
            hits.append(('fail', 'JD 要求硕士及以上；本人本科 → 不达标（09-15 人大金仓同类坑）'))
        elif kind == 'degree_phd':
            hits.append(('fail', 'JD 要求博士；本人本科 → 不达标'))
        elif kind == 'english_fluent':
            hits.append(('report', 'JD 要求英语流利/工作语言 → 按口径先报用户定（本人 CET-6 428，一律写实）'))
        elif kind == 'korean':
            hits.append(('fail', 'JD 要求韩语精通；本人无韩语 → 不达标（三星 HR 岗同类坑）'))
        elif kind == 'lang_other':
            hits.append(('fail', 'JD 要求小语种（日/西/德/法/俄）；本人只会英语 → 不匹配'))
        elif kind == 'exp3':
            hits.append(('report', 'JD 提到 %s 年经验 → 校招岗需确认是否硬性' % m.group(1)))
        elif kind == 'intern_only':
            hits.append(('excluded', '疑似实习/日薪岗 → 按排除口径出局'))
        elif kind == 'sales_commission':
            hits.append(('excluded', '疑似纯提成/地推销售岗 → 按排除口径出局'))
        else:
            hits.append(('report', msg))
    if not hits:
        return '未检出', []
    if any(k == 'fail' for k, _ in hits):
        return '不可投', hits
    if any(k == 'excluded' for k, _ in hits):
        return '排除', hits
    if any(k == 'report' for k, _ in hits):
        return '存疑', hits
    return '可投', hits


# ── 线索有效性（线索池专用）─────────────────────────────────────────────
# 池子里大量行是「侦察记录」而非可投岗位（27届未启动 / 仅实习 / 已跳过）。
# 🔴 这些行**绝不能**出现在「可投清单」里 —— 否则用户照着投会发现岗位根本不存在。
DEAD_STATUS = {'Pending', 'Skipped', 'NotFit', 'Backup'}
DEAD_TITLE_PAT = re.compile(
    r'未启动|未开启|未开始|尚未启动|待启动|预计\s*\d|暂无|无\s*2027|无2027|仅社招|只招社招|'
    r'往届|无校招|门户为空|招聘已结束|已截止|已过期')
INTERN_TITLE_PAT = re.compile(r'实习|日薪|兼职|训练营|练习生')


def liveness(row):
    """返回 (live: bool, why: str)。live=False 的线索不进可投清单。

    🔴 2026-09-27 修复：原实现只读 `row['job_title']`（**池子专用英文键**），而线索文件解析出来的是
    中文键（`岗位`/`公司`/…）→ 岗位名恒为空串，**实习/兼职/侦察记录规则对线索文件从未生效**
    （事故：「Agent 评测产品实习生」被当成可投岗，违反用户口径「不投实习」）。
    → 现统一走 `pick(row, ...)` 兼容两种键名。
    """
    st = (row.get('status') or '').strip()
    if st in DEAD_STATUS:
        return False, '线索池状态=%s（非在招岗位记录）' % st
    cohort = (pick(row, 'cohort') or '').strip()
    if cohort.lower() in ('no', 'false', '否', '不匹配'):
        return False, '届别不匹配（cohort_match_status=%s）→ 非 2027 届岗位' % cohort

    title = (pick(row, 'job') or row.get('job_title') or '').strip()
    blob = ' '.join(x for x in [title, pick(row, 'jd'), pick(row, 'notes'), pick(row, 'blocker'),
                                str(row.get('notes') or ''), str(row.get('blocker') or ''),
                                str(row.get('skip_reason') or '')] if x)
    m = DEAD_TITLE_PAT.search(blob)
    if m:
        return False, '线索为侦察记录（命中「%s」），岗位未在招' % m.group(0)

    # ① 岗位名直接带实习/兼职/日薪标记 → 一律出局（用户口径 2026-09-27「不投实习」）
    m = INTERN_TITLE_PAT.search(title)
    if m:
        return False, '岗位名含「%s」→ 排除实习/兼职/日薪岗' % m.group(0)
    # ② 岗位名没写、但 JD/备注明确是实习岗 → 出局（岗位名写「全职」的除外）
    if '全职' not in title:
        m2 = re.search(r'仅实习|纯实习|实习岗位|实习岗|实习生优先|日薪', blob)
        if m2:
            return False, 'JD/备注命中「%s」→ 排除实习岗' % m2.group(0)
    return True, ''


def scan_one(row):
    company_raw = pick(row, 'company')
    job_raw = pick(row, 'job')
    if not company_raw:
        return None
    res = verdict(company_raw, job_raw or None)
    # 门槛预检的文本 = JD + 岗位名 + 备注。
    # 单看 JD 列会漏：大量线索把语言/学历要求写在岗位名里（如「游戏本地化(日/西/德)」）
    jd = ' '.join(x for x in [pick(row, 'jd'), job_raw,
                              ' '.join(str(row.get(k, '') or '') for k in ('notes', 'skip_reason', 'blocker', 'priority'))] if x)
    gv, ghits = gate_check(jd)

    live, dead_why = liveness(row)

    quota = res.get('quota')
    if quota:
        lim = quota.get('limit')
        quota_txt = '%s/%s' % (quota.get('used'), lim if lim is not None else '?')
        if quota.get('remaining') is not None:
            quota_txt += '（剩 %s）' % quota['remaining']
    else:
        quota_txt = '未登记'

    # 综合结论（线索失效优先于一切：岗位都不在招，谈不上可投）
    if not live:
        final = '出局·非在招'
    elif res['level'] == 'company_blocked':
        final = '出局·公司级'
    elif res['level'] == 'position_dupe':
        final = '出局·岗位级'
    elif gv in ('不可投', '排除'):
        final = '出局·门槛'
    elif gv == '存疑':
        final = '存疑·待人工'
    elif res.get('needs_quota_verify'):
        final = '待核额度'
    else:
        final = '可投'

    return {
        'company_raw': company_raw,
        'company': res.get('canonical') or company_raw,
        'alias_note': ('归一化自「%s」' % company_raw) if res.get('canonical') and normalize(res['canonical']) != normalize(company_raw) else '',
        'job': job_raw,
        'city': pick(row, 'city'),
        'salary': pick(row, 'salary'),
        'platform': pick(row, 'platform'),
        'url': pick(row, 'url'),
        'level': res['level'],
        'dedup_reason': res['reason'],
        'dead_why': dead_why,
        'gate': gv,
        'gate_hits': ghits,
        'quota': quota_txt,
        'quota_obj': quota,
        'final': final,
    }


def render_md(results, src_label, args_note=''):
    today = date.today().isoformat()
    ok = [r for r in results if r['final'] == '可投']
    verify = [r for r in results if r['final'] == '待核额度']
    doubt = [r for r in results if r['final'] == '存疑·待人工']
    out = [r for r in results if r['final'].startswith('出局')]

    L = []
    L.append('# 每日扫岗 · %s' % today)
    L.append('')
    L.append('> 线索来源：`%s`%s' % (src_label, ('　｜　' + args_note) if args_note else ''))
    L.append('> 流程：第 4.1 节 8 步（归一化 → 公司级去重 → 岗位级去重 → 门槛预检 → 额度核对）')
    L.append('> 生成器：`scripts/daily_scan.py`　｜　判定底座：`dashboard/_jobhunt_common.py`')
    L.append('')
    L.append('**总计 %d 条**：可投 **%d** ｜ 待核额度 %d ｜ 存疑 %d ｜ 出局 %d' % (
        len(results), len(ok), len(verify), len(doubt), len(out)))
    if out:
        from collections import Counter as _C
        detail = ' ｜ '.join('%s %d' % (k, v) for k, v in _C(r['final'] for r in out).most_common())
        L.append('')
        L.append('> 出局构成：%s' % detail)
    L.append('')
    L.append('> 🔴 纪律：本表未出，不许开填表单。任意公司开填前仍须 `python dashboard/check_dupe.py <公司> --job "<岗位>"`。')
    L.append('')

    def table(rows):
        t = ['| 公司 | 岗位 | 城市 | 薪资 | 门槛 | 去重结论 | 额度 | 入口 | 建议动作 |',
             '|---|---|---|---|---|---|---|---|---|']
        for r in rows:
            t.append('| %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
                r['company'] + ('（%s）' % r['alias_note'] if r['alias_note'] else ''),
                r['job'] or '—', r['city'] or '—', r['salary'] or '—',
                r['gate'], r['dedup_reason'], r['quota'],
                (r['url'][:60] if r['url'] else '—'),
                '读 JD 定制定制简历 → 填到待提交' if r['final'] == '可投' else '—'))
        return t

    L.append('## 一、✅ 可投清单（%d）' % len(ok))
    L.append('')
    L.extend(table(ok) if ok else ['（无）'])
    L.append('')
    L.append('## 二、🔍 待核额度（%d）—— 岗位本身可投，但**该站额度未登记**，开填前须先站内核对剩余志愿数' % len(verify))
    L.append('')
    if verify:
        L.extend(table(verify))
        L.append('')
        L.append('> ⚠️ 这类条目的风险：站内可能已限投满额（SHEIN 限投 3 个、携程限投 1 岗都是这样漏掉的）。')
        L.append('> 核对后请把结果写回 `dashboard/site_quotas.json`，下次即自动判定。')
    else:
        L.append('（无）')
    L.append('')
    L.append('## 三、⚠️ 存疑（%d）—— 需人工读 JD / 报用户' % len(doubt))
    L.append('')
    if doubt:
        for r in doubt:
            L.append('- **%s · %s**（%s）→ %s' % (r['company'], r['job'] or '—', r['city'] or '—', r['gate']))
            for k, m in r['gate_hits']:
                L.append('    - `%s` %s' % (k, m))
            L.append('    - 去重：%s ｜ 额度：%s' % (r['dedup_reason'], r['quota']))
    else:
        L.append('（无）')
    L.append('')
    L.append('## 四、⛔ 出局（%d）—— 附依据（可追溯，勿再反复问）' % len(out))
    L.append('')
    if out:
        L.append('| 公司 | 岗位 | 出局类型 | 依据 |')
        L.append('|---|---|---|---|')
        for r in out:
            reason = r['dedup_reason']
            if r['final'] == '出局·门槛':
                reason = '；'.join(m for _, m in r['gate_hits'])
            elif r['final'] == '出局·非在招':
                reason = r['dead_why']
            L.append('| %s | %s | %s | %s |' % (r['company'], r['job'] or '—', r['final'], reason))
    else:
        L.append('（无）')
    L.append('')
    L.append('## 五、口径自检')
    L.append('')
    L.append('- 本次扫描**未使用任何城市排除规则**（北京已解锁；城市口径 = 不限城市，薪资达标即可）')
    L.append('- 公司级判定 = 满额 / 被拦 / 已结束；岗位级判定 = 同公司 + 同岗位')
    L.append('- 门槛预检只认「明确写在 JD 里」的硬门槛，「未检出」≠ 达标，仍需人工读 JD')
    L.append('')
    return '\n'.join(L)


def rebuild_resume_index():
    d = os.path.join(PROJECT_ROOT, 'my-materials', 'resume-qiuzhao')
    if not os.path.isdir(d):
        print('❌ 简历目录不存在：%s' % d)
        return 1
    pdfs = sorted([f for f in os.listdir(d) if f.lower().endswith('.pdf')])
    htmls = {os.path.splitext(f)[0] for f in os.listdir(d) if f.lower().endswith('.html')}
    builds = sorted([f for f in os.listdir(d) if f.startswith('_build_') and f.endswith('.py')])
    items = []
    for f in pdfs:
        stem = os.path.splitext(f)[0]
        p = os.path.join(d, f)
        items.append({
            'file': f,
            'html': (stem + '.html') if stem in htmls else '',
            'mtime': date.fromtimestamp(os.path.getmtime(p)).isoformat(),
            'size_kb': int(os.path.getsize(p) / 1024),
        })
    out = {
        'generated': date.today().isoformat(),
        'dir': 'my-materials/resume-qiuzhao',
        'count_pdf': len(pdfs),
        'count_html': len(htmls),
        'build_scripts': builds,
        'resumes': items,
    }
    dest = os.path.join(PROJECT_ROOT, 'dashboard', '简历索引_全量.json')
    with open(dest, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('✅ 简历索引已重建：%s' % dest)
    print('   PDF %d 份 ｜ HTML %d 份 ｜ 构建脚本 %d 个' % (len(pdfs), len(htmls), len(builds)))
    return 0


def main():
    argv = sys.argv[1:]
    if '--resumes' in argv:
        return rebuild_resume_index()

    out_path, as_json, limit = None, '--json' in argv, 0  # limit=0 → 不截断（2026-09-28 修复）
    src_files, inline = [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--pool':
            src_files.append('__POOL__')
        elif a == '--out':
            i += 1
            out_path = argv[i] if i < len(argv) else None
        elif a == '--limit':
            i += 1
            try:
                limit = int(argv[i])
            except Exception:
                pass
        elif a == '--inline':
            i += 1
            while i < len(argv) and not argv[i].startswith('--'):
                inline.append(argv[i])
                i += 1
            continue
        elif a in ('--json',):
            pass
        elif a in ('-h', '--help'):
            print(__doc__)
            return 0
        elif not a.startswith('--'):
            src_files.append(a)
        i += 1

    rows, labels = [], []
    for sf in src_files:
        if sf == '__POOL__':
            rows += load_pool()
            labels.append('job_pool.csv')
            continue
        if not os.path.exists(sf):
            print('⚠️ 线索文件不存在，跳过：%s' % sf, file=sys.stderr)
            continue
        with open(sf, encoding='utf-8-sig', errors='replace') as f:
            rows += sniff_table(f.read())
        labels.append(sf)
    for t in inline:
        rows += sniff_table(t)
    if inline:
        labels.append('--inline')

    if not rows:
        print('❌ 未解析到任何线索行。')
        print(__doc__)
        return 1

    # 只保留有公司列的行
    rows = [r for r in rows if pick(r, 'company')]
    if not rows:
        print('❌ 线索行的列名无法识别出「公司」。支持的表头：%s' % '、'.join(COL_MAP['company']))
        return 1
    # 🔴 2026-09-28 修复：原为 `rows = rows[:limit]`（默认 limit=200），而 job_pool.csv 已 287 行
    #    ⇒ **后 87 行被静默截断**（伊利/农夫山泉/蒙牛/龙湖 等新行全在被截断部分，扫岗假阴性）。
    #    现改为：limit<=0 表示不限制（默认），只有显式传 --limit N 才截断。
    if limit and limit > 0:
        rows = rows[:limit]

    results = []
    seen = set()
    for r in rows:
        res = scan_one(r)
        if not res:
            continue
        key = (normalize(res['company']), normalize_job(res['job']))
        if key in seen:
            continue
        seen.add(key)
        results.append(res)

    results.sort(key=lambda r: (
        {'可投': 0, '待核额度': 1, '存疑·待人工': 2}.get(r['final'], 3),
        r['company']))

    md = render_md(results, ' + '.join(labels) if labels else '（未指定）')
    if out_path is None:
        out_path = os.path.join(PROJECT_ROOT, 'dashboard', '每日扫岗_%s.md' % date.today().isoformat())
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(md)

    ok = sum(1 for r in results if r['final'] == '可投')
    ver = sum(1 for r in results if r['final'] == '待核额度')
    print('✅ 扫岗完成：共 %d 条 → 可投 %d ｜ 待核额度 %d ｜ 存疑 %d ｜ 出局 %d' % (
        len(results), ok, ver,
        sum(1 for r in results if r['final'] == '存疑·待人工'),
        sum(1 for r in results if r['final'].startswith('出局'))))
    print('📄 输出：%s' % out_path)

    if as_json:
        jp = os.path.splitext(out_path)[0] + '.json'
        slim = [{k: v for k, v in r.items() if k != 'quota_obj'} for r in results]
        with open(jp, 'w', encoding='utf-8') as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
        print('🧾 JSON：%s' % jp)

    # 控制台摘要：只打可投 / 待核额度
    if ok:
        print('\n── ✅ 可投清单 ──')
        for r in results:
            if r['final'] == '可投':
                print('  ✅ %-18s %-34s %-10s 额度 %s' % (
                    r['company'], (r['job'] or '—')[:32], r['city'] or '—', r['quota']))
    if ver:
        print('\n── 🔍 待核额度（开填前先站内核对剩余志愿数）──')
        for r in results:
            if r['final'] == '待核额度':
                print('  🔍 %-18s %-34s %-10s 额度 %s' % (
                    r['company'], (r['job'] or '—')[:32], r['city'] or '—', r['quota']))
    return 0


if __name__ == '__main__':
    sys.exit(main())

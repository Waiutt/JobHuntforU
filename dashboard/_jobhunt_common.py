# -*- coding: utf-8 -*-
"""
_jobhunt_common.py — 查重/扫岗共用底座（company_aliases + site_quotas + 台账）

设计要点（来自 09-15 两次翻车的教训）：
  1. 公司名归一化必须先剥离平台前缀与括号内容，再精确命中别名
  2. 🔴 禁止模糊前缀匹配 —— 「中国电子云」曾被前缀匹配到「中国电信」
  3. 两级判定：公司级（满额/被拦/已结束）→ 禁止；岗位级（同公司同岗位）→ 换岗可投
"""
import csv
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE)

LOG = os.path.join(BASE, 'application_log.csv')
POOL = os.path.join(BASE, 'job_pool.csv')
ALIASES_PATH = os.path.join(BASE, 'company_aliases.json')
QUOTAS_PATH = os.path.join(BASE, 'site_quotas.json')

# ── 状态分级（第 4.2 节）────────────────────────────────────────────────
# 公司级：被拦 / 已结束 → 整家公司出局
COMPANY_LEVEL_STATUS = {'Blocked', 'Rejected', 'Terminated'}
# 岗位级：仅该岗位出局，同公司其他岗可投
POSITION_LEVEL_STATUS = {'Submitted', 'Assessment', 'Interview', 'Offer', 'ApplicationUpdated'}
# 全部「占用」状态（= TAKEN，兼容旧行为）
TAKEN = COMPANY_LEVEL_STATUS | POSITION_LEVEL_STATUS
# 非占用但需人工判断
NEEDS_REVIEW_STATUS = {'NeedsUser', 'ToApply'}


# ── 归一化 ──────────────────────────────────────────────────────────────
_PUNCT = re.compile(r'[\s\t　（）()\[\]【】·・,，.。:：;；\-—–_/\\|"\'“”‘’]')


def _load_alias_config():
    with open(ALIASES_PATH, encoding='utf-8') as f:
        return json.load(f)


def _load_quota_config():
    with open(QUOTAS_PATH, encoding='utf-8') as f:
        return json.load(f)


_ALIAS_CFG = None
_QUOTA_CFG = None


def alias_config():
    global _ALIAS_CFG
    if _ALIAS_CFG is None:
        _ALIAS_CFG = _load_alias_config()
    return _ALIAS_CFG


def quota_config():
    global _QUOTA_CFG
    if _QUOTA_CFG is None:
        _QUOTA_CFG = _load_quota_config()
    return _QUOTA_CFG


def strip_platform_prefix(name):
    """剥离聚合平台前缀：牛客网-满帮集团 → 满帮集团"""
    s = (name or '').strip()
    for p in alias_config().get('strip_platform_prefixes', []):
        if s.startswith(p):
            s = s[len(p):].strip()
    return s


def strip_parens(name):
    """剥离括号内容（含中文/英文括号），保留括号前主体"""
    s = re.sub(r'[（(][^）)]*[）)]', '', name or '')
    return s.strip()


def normalize(name):
    """归一化：去平台前缀 → 去括号 → 去标点空白 → 转小写"""
    s = strip_platform_prefix(name)
    s = strip_parens(s)
    s = _PUNCT.sub('', s)
    return s.lower()


def build_alias_index():
    """构造 {归一化别名: (canonical, 原始别名, 长度)}"""
    idx = {}
    for key, spec in alias_config().get('aliases', {}).items():
        items = list(spec.get('aliases', [])) + [key]
        for a in items:
            n = normalize(a)
            if not n:
                continue
            # 同一归一化键冲突时，保留更长的原名（信息量更大）
            if n not in idx or len(a) > len(idx[n][1]):
                idx[n] = (key, a, len(n))
    return idx


_ALIAS_INDEX = None


def alias_index():
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        _ALIAS_INDEX = build_alias_index()
    return _ALIAS_INDEX


def _apply_collision_guards(raw_norm, matched_canonicals):
    """
    碰撞守卫：命中多个 canonical 时，按 must_not_match 排除。
    例：输入「中国电子云」→ 归一化后含「中国电信」→ 但守卫声明二者不同 → 排除中国电信。
    """
    if len(matched_canonicals) <= 1:
        return matched_canonicals
    out = set(matched_canonicals)
    for guard in alias_config().get('collision_guards', []):
        if guard.get('disable'):
            continue
        pat = normalize(guard.get('pattern', ''))
        if not pat or pat not in raw_norm:
            continue
        # 本守卫只在「输入确实以 pattern 为主体」时生效
        if not raw_norm.startswith(pat):
            continue
        for bad in guard.get('must_not_match', []):
            bn = normalize(bad)
            for c in list(out):
                if normalize(c) == bn or normalize(c).startswith(bn):
                    out.discard(c)
    return out


def suggest_canonical(name):
    """
    兜底规范化：别名表未收录时的确定性回退（**非**模糊前缀匹配）。
    用于让「携程计算机技术(上海)有限公司(携程集团)」与「携程计算机技术(上海)有限公司」
    解析到同一 canonical，而无需为台账里 200+ 种写法逐个手抄别名。

    规则：
      1. 剥平台前缀 → 剥括号（括号里若含 集团/公司/股份 等后缀，则整段丢弃；
         否则视为别名，单独保留用于变体匹配）
      2. 去掉地域前缀（上海/北京/...）
      3. 去空白标点

    🔴 与模糊前缀匹配的区别：本函数只**裁剪修饰成分**，不改变主体词，
       因此「中国电子云」永远不会被裁成「中国电信」。
    """
    s = strip_platform_prefix(name)
    extras = []
    for m in re.findall(r'[（(]([^）)]*)[）)]', s):
        m = m.strip()
        # 纯后缀型括号（集团/公司/股份/科技…）直接丢；别名型保留
        if m and not re.search(r'(集团|公司|股份|有限|控股|事业群|事业部)$', m) and len(m) <= 12:
            extras.append(m)
    s = strip_parens(s)
    s = re.sub(r'^(上海|北京|广州|深圳|杭州|南京|苏州|成都|武汉|西安|天津|重庆|宁波|厦门|长沙|青岛|合肥|郑州|无锡|福州|东莞|佛山|珠海|中山|大连|沈阳|济南|昆明|南昌|贵阳|太原|石家庄|哈尔滨|长春|兰州|乌鲁木齐|呼和浩特|南宁|海口|香港|中国)', '', s)
    core = _PUNCT.sub('', s)
    return core, [normalize(e) for e in extras]


_FALLBACK_CACHE = {}


def resolve_company(name):
    """
    把任意写法解析为 canonical。
    返回 dict: {canonical, matched_alias, method, input, candidates}
    method: exact | contains | fallback | none
    """
    raw_norm = normalize(name)
    input_display = strip_platform_prefix(name)

    if not raw_norm:
        return {'canonical': None, 'matched_alias': None, 'method': 'none',
                'input': input_display, 'candidates': []}

    idx = alias_index()

    # 1) 精确命中
    if raw_norm in idx:
        canon, orig, _ = idx[raw_norm]
        return {'canonical': canon, 'matched_alias': orig, 'method': 'exact',
                'input': input_display, 'candidates': [canon]}

    # 2) 包含命中（别名整体出现在输入里），且别名长度 >= 4
    direct = []
    for an, (canon, orig, ln) in idx.items():
        if ln >= 4 and ln < len(raw_norm) and an in raw_norm:
            direct.append((ln, canon, orig))
    if direct:
        direct.sort(reverse=True)
        cands = list(dict.fromkeys(c for _, c, _ in direct))
        cands = sorted(_apply_collision_guards(raw_norm, cands))
        best = direct[0]
        return {'canonical': best[1], 'matched_alias': best[2], 'method': 'contains',
                'input': input_display, 'candidates': cands}

    # 3) 兜底规范化（确定性裁剪，非模糊前缀匹配）
    core, extras = suggest_canonical(name)
    if core:
        # 若某个已登记的 canonical/别名 与 core 完全相等，仍算精确命中
        if core in idx:
            canon, orig, _ = idx[core]
            return {'canonical': canon, 'matched_alias': orig, 'method': 'exact',
                    'input': input_display, 'candidates': [canon]}
        return {'canonical': core, 'matched_alias': None, 'method': 'fallback',
                'input': input_display, 'candidates': [core], 'extras': extras}

    return {'canonical': None, 'matched_alias': None, 'method': 'none',
            'input': input_display, 'candidates': []}


def same_company(a, b):
    """两个公司名写法是否指向同一家（用于台账 blob 过滤的精确化）"""
    ca, cb = resolve_company(a).get('canonical'), resolve_company(b).get('canonical')
    if ca and cb:
        return ca == cb
    return normalize(a) == normalize(b) and bool(normalize(a))


# ── 数据加载 ────────────────────────────────────────────────────────────
def load_csv(path):
    if not os.path.exists(path):
        return [], []
    # utf-8-sig：台账首列带 BOM（\ufeffattempt_date），用 utf-8 会取不到日期
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    hdr = [(h or '').lstrip('\ufeff') for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any((c or '').strip() for c in r):
            continue
        out.append(dict(zip(hdr, r + [''] * (len(hdr) - len(r)))))
    return hdr, out


def load_ledger():
    _, rows = load_csv(LOG)
    return rows


def load_pool():
    _, rows = load_csv(POOL)
    return rows


def normalize_job(title):
    """
    岗位名归一化：去掉城市/全职/届别等后缀噪音，只留核心岗位名。
    「【英才2027】用户运营（Hello100）-两轮出行事业部(J21746)（2027届校招·全职）」
      → 用户运营hello100两轮出行事业部j21746
    """
    s = title or ''
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    s = re.sub(r'【[^】]*】', '', s)
    s = _PUNCT.sub('', s)
    return s.lower()


def job_matches(query_title, ledger_title):
    """岗位级重复判定：归一化后相等，或一方完整包含另一方且长度 >= 4"""
    a, b = normalize_job(query_title), normalize_job(ledger_title)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 4 and a in b:
        return True
    if len(b) >= 4 and b in a:
        return True
    return False


# ── 额度 ────────────────────────────────────────────────────────────────
def quota_for(canonical):
    """
    按 canonical 找额度条目。
    🔗 关键：台账里同一家公司有多种写法（「携程」/「携程计算机技术(上海)有限公司」），
    且兜底规范化会产出不同的 canonical → 因此必须**双向比对**：
      条目键 == canonical  或  条目键的兜底规范化 == canonical 的兜底规范化
    这样一条额度记录即可覆盖全部写法变体。
    """
    if not canonical:
        return None
    target = normalize(canonical)
    target_core, _ = suggest_canonical(canonical)
    for q in quota_config().get('quotas', []):
        for key in (q.get('canonical'), q.get('company')):
            if not key:
                continue
            nk = normalize(key)
            if nk == target:
                return q
            if target_core and suggest_canonical(key)[0] == target_core:
                return q
            # 前缀主体匹配（「携程」⊂「携程计算机技术有限公司」）
            # 🔴 必须 >= 4 字：短前缀是最危险的一类模糊匹配（中国电子云 vs 中国电信 就是 3 字前缀）
            if len(nk) >= 4 and nk in target:
                return q
    return None


def is_company_level_blocklisted(canonical):
    if not canonical:
        return None
    target = normalize(canonical)
    target_core, _ = suggest_canonical(canonical)
    for b in quota_config().get('company_level_blocked', []):
        for key in (b.get('canonical'), b.get('company')):
            if not key:
                continue
            nk = normalize(key)
            if nk == target or (target_core and suggest_canonical(key)[0] == target_core):
                return b
            if len(nk) >= 4 and nk in target:
                return b
    return None


# ── 两级判定核心 ────────────────────────────────────────────────────────
def gather_hits(canonical, aliases_to_search):
    """
    在台账+池子里找出属于该 canonical 的所有行。
    🔴 必须与 resolve_company 的兜底规范化一致地比对 ——
       否则「携程计算机技术(上海)有限公司(携程集团)」这类写法会被漏掉。
    """
    hits = []
    target_core = suggest_canonical(canonical)[0] if canonical else None
    for src, rows in (('application_log.csv', load_ledger()), ('job_pool.csv', load_pool())):
        for d in rows:
            company = d.get('company', '')
            rc = resolve_company(company)
            matched = False
            if canonical:
                row_canon = rc.get('canonical')
                if row_canon == canonical:
                    matched = True
                elif target_core and suggest_canonical(row_canon or '')[0] == target_core:
                    # 变体写法：主体词一致即同一家
                    matched = True
                elif target_core and len(target_core) >= 3 and target_core in normalize(company):
                    matched = True
            else:
                low = normalize(company)
                matched = any(normalize(a) and normalize(a) in low for a in aliases_to_search)
            if matched:
                hits.append({
                    'src': src,
                    'date': d.get('attempt_date') or d.get('date_found') or d.get('date', ''),
                    'company': company,
                    'job': d.get('job_title', ''),
                    'platform': d.get('platform') or d.get('source', ''),
                    'status': (d.get('status') or '').strip(),
                    'notes': (d.get('notes', '') or '')[:160],
                    'recon': is_recon_row(src, d),
                    'row': d,
                })
    return hits


def is_recon_row(src, row):
    """
    job_pool 里的「侦察记录」：仅表示"这家已投过"，其 job_title 是整个批次的汇总描述，
    **不是**某个具体投递岗位 → 不能用于岗位级去重（否则产生假重复）。

    实证：哈啰 pool 行 job_title=「2027届校招·运营管培生 / 线上运营（另有商业分析、产品经理）」
          status=Submitted，而真实已投岗位是「用户运营（Hello100 J21746）」
          → 若不排除，`check_dupe 哈啰 --job 运营管培生` 会误判为已投。
    """
    if 'pool' not in src:
        return False
    markers = alias_config().get('pool_row_policy', {}).get('recon_markers', [])
    blob = ' '.join(str(row.get(k, '') or '') for k in ('job_title', 'notes', 'blocker', 'next_action'))
    return any(m and m in blob for m in markers)


def verdict(query, job_title=None):
    """
    两级判定。
    query    : 公司名任意写法（电科金仓 / 小川在线 / FunPlus / 牛客网-满帮集团）
    job_title: 可选，待投岗位名 → 触发岗位级判定

    返回 dict:
      level  : 'company_blocked' | 'position_dupe' | 'clear'
      reason : 人类可读原因
      ...
    """
    res = resolve_company(query)
    canonical = res['canonical']
    search_aliases = [query] + ([canonical] if canonical else [])
    if canonical:
        spec = alias_config().get('aliases', {}).get(canonical, {})
        search_aliases += list(spec.get('aliases', []))

    hits = gather_hits(canonical, search_aliases)
    taken = [h for h in hits if h['status'] in TAKEN]
    company_level = [h for h in hits if h['status'] in COMPANY_LEVEL_STATUS]
    # 岗位级去重只看**真实投递记录**（侦察记录 job_title 是批次汇总，不能当岗位名）
    position_level = [h for h in hits
                      if h['status'] in POSITION_LEVEL_STATUS and not h.get('recon')]
    review = [h for h in hits if h['status'] in NEEDS_REVIEW_STATUS and not h.get('recon')]

    quota = quota_for(canonical)
    blocked_entry = is_company_level_blocklisted(canonical)

    out = {
        'query': res['input'],
        'canonical': canonical,
        'match_method': res['method'],
        'matched_alias': res['matched_alias'],
        'candidates': res['candidates'],
        'hits': hits,
        'taken': taken,
        'company_level': company_level,
        'position_level': position_level,
        'review': review,
        'quota': quota,
        'blocked_entry': blocked_entry,
        'level': 'clear',
        'reason': '',
    }

    # —— 公司级第一优先 ——
    if blocked_entry:
        out['level'] = 'company_blocked'
        out['reason'] = '%s（%s）' % (blocked_entry.get('reason', ''), blocked_entry.get('type', ''))
        return out

    if quota and quota.get('status') == 'quota_exhausted':
        out['level'] = 'company_blocked'
        out['reason'] = '额度已满 %s/%s' % (quota.get('used'), quota.get('limit'))
        return out

    # 🔴 并发上限：有的站点「总额度未满」但「同时只能有 1 个申请在流程中」
    #    （哈啰官方原文：「最多可投递3个职位，但同时仅有1个投递申请可在应聘流程中」）
    #    这类额度没满也**投不了**，必须单独拦。
    alim, aused = quota.get('active_limit') if quota else None, quota.get('active_used') if quota else None
    if alim is not None and aused is not None and aused >= alim:
        out['level'] = 'company_blocked'
        out['reason'] = ('并发上限：同时仅允许 %s 个申请在流程中，当前已有 %s 个进行中 → '
                         '须等现有申请出结果后才能投第 2 个（总额度 %s/%s 未满也无用）'
                         % (alim, aused, quota.get('used'), quota.get('limit')))
        out['active_blocked'] = True
        return out

    if company_level:
        st = sorted({h['status'] for h in company_level})
        out['level'] = 'company_blocked'
        out['reason'] = '存在公司级占用状态 %s（被拦/已结束）' % '/'.join(st)
        return out

    # —— 岗位级第二优先 ——
    if job_title:
        dupes = [h for h in position_level if job_matches(job_title, h['job'])]
        if dupes:
            out['level'] = 'position_dupe'
            out['dupe_hits'] = dupes
            out['reason'] = '该岗位已投递过：%s' % dupes[0]['job'][:60]
            return out

    # —— 通过 ——
    if position_level and job_title:
        out['level'] = 'clear'
        if quota and quota.get('limit') is not None:
            out['reason'] = '公司已投 %d 笔但额度未满（%s/%s），且本岗为新岗位 → 可投' % (
                len(position_level), quota.get('used'), quota.get('limit'))
        else:
            # 额度未登记 或 上限未探明 + 已有投递 → 不能给无条件的"可投"
            used_note = ('历史已投 %s 笔' % quota.get('used')) if quota else '额度未登记'
            out['needs_quota_verify'] = True
            out['reason'] = ('公司已投 %d 笔、本岗为新岗位；⚠️ 但该站%s、上限未探明，'
                             '开填前须先在站内核对剩余志愿数' % (len(position_level), used_note))
        return out

    if position_level:
        out['level'] = 'clear'
        out['needs_quota_verify'] = not quota
        out['reason'] = '公司已投 %d 笔，额度未满，仅需岗位级去重（未提供岗位名，请补岗位名再判）' % len(position_level)
        return out

    if quota and quota.get('status') == 'still_open':
        out['level'] = 'clear'
        remaining = quota.get('remaining')
        if remaining is None and quota.get('limit') is not None:
            remaining = quota['limit'] - quota.get('used', 0)
        out['reason'] = '台账无记录；站点额度 %s/%s%s' % (
            quota.get('used'), quota.get('limit'),
            ('，剩 %s' % remaining) if remaining is not None else '')
        return out

    if quota and quota.get('status') == 'mind_limit':
        out['level'] = 'clear'
        out['needs_quota_verify'] = True
        out['reason'] = '台账无记录；⚠️ 该站额度未登记（历史已投 %s 笔），开填前须站内核对剩余志愿数' % quota.get('used')
        return out

    if review:
        out['level'] = 'clear'
        out['reason'] = '⚠️ 有 %d 条非占用记录（%s）需人工判断' % (
            len(review), '/'.join(sorted({h['status'] for h in review})))
        return out

    out['level'] = 'clear'
    out['reason'] = '台账无记录 —— 可以开投'
    return out


# ── 别名预检（防止"查了 A 却漏了 A 的别名"）─────────────────────────────
def alias_sidecar_notes(res):
    """如果该公司在台账里的其他别名已有占用记录，给出提示"""
    notes = []
    canonical = res.get('canonical')
    if not canonical:
        return notes
    spec = alias_config().get('aliases', {}).get(canonical, {})
    aliases = spec.get('aliases', [])
    seen_companies = {h['company'] for h in res.get('hits', [])}
    for a in aliases:
        na = normalize(a)
        if not na:
            continue
        for src_name in seen_companies:
            if na in normalize(src_name) and normalize(a) != normalize(res['query']):
                notes.append('别名「%s」在台账中的写法：%s' % (a, src_name))
    return list(dict.fromkeys(notes))

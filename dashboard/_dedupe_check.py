# -*- coding: utf-8 -*-
"""投前查重（精确 + 归一化模糊）：
    python dashboard/_dedupe_check.py 关键词1 [关键词2 ...]
行为：① 子串匹配（公司/岗位/URL 全含关键词）；② 归一化匹配（公司名剔除城市与组织后缀、
岗位名剔除「（第N志愿）」「（校招）」等修饰后再比对）。命中打印明细并返回退出码 1。
"""
import csv, io, os, re, sys, json

D = os.path.dirname(os.path.abspath(__file__))
FILES = [('台账 application_log', 'application_log.csv'), ('池 job_pool', 'job_pool.csv'), ('未投递 unsubmitted_log', 'unsubmitted_log.csv')]
CITIES = '成都|上海|广州|深圳|北京|武汉|杭州|南京|天津|重庆|苏州|宁波|西安|长沙|郑州|合肥|福州|厦门|青岛|济南|大连|沈阳|哈尔滨|昆明|贵阳|南宁|海口|石家庄|太原|南昌|兰州|银川|西宁|乌鲁木齐|呼和浩特|拉萨|无锡|佛山|东莞|珠海|中山|惠州|汕头|温州|嘉兴|绍兴|金华|台州|泉州|常州|徐州|南通|扬州|盐城|泰州|镇江|芜湖|洛阳|襄阳|株洲|湘潭|衡阳|岳阳|常德|郴州|柳州|桂林|三亚'
SUFFIX = '股份有限公司|有限责任公司|有限公司|股份公司|分公司|子公司|子集团|集团|公司|分部|办事处|中国|国际'

def norm_c(c):
    c = re.sub(r'[（(].*?[)）]', '', c or '')
    c = re.sub(r'(%s)' % CITIES, '', c)
    c = re.sub(r'(%s)' % SUFFIX, '', c)
    return re.sub(r'[\s·\-—_]+', '', c)

def norm_t(t):
    t = re.sub(r'[（(][^)）]*志愿[)）]', '', t or '')
    t = re.sub(r'[（(](20\d\d届?校招|校招|全职|实习|第\d志愿|\d+届|MJ\d+)[)）]', '', t)
    return re.sub(r'[\s\-—_/]+', '', t).lower()

keys = [k.strip() for k in sys.argv[1:] if k.strip()]
if not keys:
    print('用法: python _dedupe_check.py 关键词1 [关键词2 ...]（多关键词为 AND）'); sys.exit(2)

rows_all = []
for label, fn in FILES:
    p = os.path.join(D, fn)
    if not os.path.exists(p):
        continue
    with io.open(p, encoding='utf-8-sig', newline='') as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            rows_all.append((label, i, r))

hits = []
for label, i, r in rows_all:
    blob = ' '.join([str(r.get('company') or ''), str(r.get('job_title') or ''), str(r.get('job_url') or '')])
    if all(k in blob for k in keys):
        hits.append(('精确子串', label, i, r))

# 归一化匹配：关键词归一化后需同时出现在 归一化公司名 或 归一化岗位名 中
nk = [norm_c(k) for k in keys]
if len(nk) == 1:
    nk_c = nk[0]
    for label, i, r in rows_all:
        if any(h[2] == i and h[1] == label for h in hits):
            continue
        if nk_c and (nk_c in norm_c(r.get('company') or '') or nk_c in norm_t(r.get('job_title') or '')):
            hits.append(('归一化模糊', label, i, r))

for kind, label, i, r in hits:
    print('[%s|%s] 行 %d | %s | %s | 状态 %s | 日期 %s | %s' % (
        kind, label, i, (r.get('company') or '')[:26], (r.get('job_title') or '')[:30],
        (r.get('status') or ''), (r.get('attempt_date') or r.get('date_found') or ''), (r.get('job_url') or '')[:56]))
print('命中 %d 条 ｜ 关键词: %s' % (len(hits), ' + '.join(keys)))
sys.exit(1 if hits else 0)

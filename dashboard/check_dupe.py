# -*- coding: utf-8 -*-
"""
check_dupe.py — 开投前查重（强制第一步）· 两级判定版

用法:
    python -X utf8 check_dupe.py 三星 金蝶 趣加
    python -X utf8 check_dupe.py 趣加 --job "海外广告投放优化师"
    python -X utf8 check_dupe.py 电科金仓 小川在线 FunPlus   # 别名自动归一化

判定分两级（第 4.2 节口径）:
    公司级 = Blocked / Rejected / Terminated / 额度已满  → ⛔ 整家公司禁止开填
    岗位级 = Submitted / Assessment / Interview / Offer / ApplicationUpdated
             → 仅「同公司 + 同岗位」禁止；同公司其他岗可投

退出码:
    0 = 全部可投（含"公司已投但额度未满、换岗可投"）
    2 = 存在公司级禁止（禁止开填，先与用户确认）
    1 = 参数错误
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认 GBK，emoji 会抛 UnicodeEncodeError 中断查重 → 强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from _jobhunt_common import verdict, alias_config, normalize  # noqa: E402

ICON = {'company_blocked': '⛔', 'position_dupe': '🚫', 'clear': '✅'}
LABEL = {'company_blocked': '公司级禁止', 'position_dupe': '岗位级重复', 'clear': '可投'}


def parse_args(argv):
    queries, job = [], None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('--job', '-j'):
            i += 1
            job = argv[i] if i < len(argv) else None
        elif a.startswith('--job='):
            job = a.split('=', 1)[1]
        elif a in ('-h', '--help'):
            print(__doc__)
            sys.exit(0)
        else:
            queries.append(a)
        i += 1
    return queries, job


def show_quota(q):
    if not q:
        return
    lim = q.get('limit')
    usage = '%s/%s' % (q.get('used'), lim if lim is not None else '?')
    extra = ''
    if q.get('remaining') is not None:
        extra = '，剩 %s' % q['remaining']
    if q.get('status') == 'quota_exhausted':
        extra = '  ← 已满'
    print('  📊 站点额度：%s ／ %s%s' % (usage, q.get('site', ''), extra))
    if q.get('note'):
        print('      %s' % q['note'])
    print('      周期：%s ｜ 状态：%s' % (q.get('period', '?'), q.get('status', '?')))


def main():
    queries, job = parse_args(sys.argv[1:])
    if not queries:
        print(__doc__)
        return 1

    blocked_any = False
    for kw in queries:
        res = verdict(kw, job)
        lvl = res['level']
        print('=' * 66)
        print('%s 查重：%s' % (ICON[lvl], kw))
        if res['canonical'] and normalize(res['canonical']) != normalize(kw):
            print('  🔗 归一化 → 「%s」（命中别名：%s，方式：%s）' % (
                res['canonical'], res['matched_alias'], res['match_method']))
        elif not res['canonical']:
            print('  ⚠️ 别名表未收录该公司名 —— 使用窄匹配，建议补进 company_aliases.json')
        if len(res['candidates']) > 1:
            print('  ⚠️ 命中多个候选：%s' % '、'.join(res['candidates']))

        show_quota(res['quota'])

        if res['hits']:
            print('  ── 台账记录 %d 条 ──' % len(res['hits']))
            for h in res['hits'][:30]:
                if h['status'] in ('Blocked', 'Rejected', 'Terminated'):
                    flag = '⛔'
                elif h['status'] in ('Submitted', 'Assessment', 'Interview', 'Offer', 'ApplicationUpdated'):
                    flag = '🚫'
                else:
                    flag = '·'
                print('    %s [%s] %s | %s | %s | %s' % (
                    flag, h['src'].replace('.csv', ''), h['date'], h['company'], h['job'], h['status']))
                if h['notes']:
                    print('        备注: %s' % h['notes'])
            if len(res['hits']) > 30:
                print('    … 其余 %d 条省略' % (len(res['hits']) - 30))
        else:
            print('  （台账无该公司记录）')

        print('  ── 判定：%s ──' % LABEL[lvl])
        print('  %s %s' % (ICON[lvl], res['reason']))
        if job:
            print('  🎯 待投岗位：%s' % job)

        if lvl == 'company_blocked':
            print('  ⛔ 禁止开填表单，先与用户确认')
            blocked_any = True
        elif lvl == 'position_dupe':
            print('  🚫 该岗位已投；同公司其他岗位仍可投（额度未满）')
            print('     ➡️ 换岗后重跑：python check_dupe.py %s --job "<新岗位名>"' % kw)

    print('=' * 66)
    if blocked_any:
        print('结论：⛔ 存在公司级禁止记录 —— 停止开填')
    else:
        print('结论：✅ 全部可投')
    return 2 if blocked_any else 0


if __name__ == '__main__':
    sys.exit(main())

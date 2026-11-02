# -*- coding: utf-8 -*-
"""台账完整性校验 —— 每次写入后必跑。

检查项（对应实战规范「原则 14 台账写入」）：
  1. BOM 是否保留（Excel 双击不乱码）
  2. 行数
  3. 16 列表头是否与约定一致
  4. 是否有畸形行（列数不符）
  5. 是否有重复行（company + job_title + attempt_date）
  6. 关键字段是否为空（attempt_date / company / job_title / status）
  7. 状态分布（便于一眼看出漏斗）

用法:
    python scripts/verify_ledger.py
"""
import csv, io, os, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DASH = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(DASH, 'application_log.csv')

HDR = ['attempt_date', 'company', 'job_title', 'job_url', 'platform', 'status',
       'submission_evidence', 'resume_used', 'answers_used', 'confirmation_url',
       'confirmation_text', 'job_description', 'notes', 'stage_detail',
       'outcome_reason', 'stage_updated_at']


def main():
    if not os.path.exists(CSV_PATH):
        raise SystemExit('❌ 找不到台账: %s' % CSV_PATH)

    raw = open(CSV_PATH, 'rb').read()
    print('BOM preserved      :', raw.startswith(b'\xef\xbb\xbf'))
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    print('row count          :', len(rows))
    print('header matches     :', list(rows[0].keys()) == HDR if rows else 'EMPTY')

    bad = [i for i, r in enumerate(rows, 2) if None in r or any(k is None for k in r)]
    print('malformed rows     :', bad if bad else 'none')

    seen, dupes = {}, []
    for i, r in enumerate(rows, 2):
        k = (r.get('company'), r.get('job_title'), r.get('attempt_date'))
        if k in seen:
            dupes.append((i, seen[k], k))
        seen[k] = i
    print('duplicate rows     :', dupes if dupes else 'none')

    for col in ('attempt_date', 'company', 'job_title', 'status'):
        empt = [i for i, r in enumerate(rows, 2) if not (r.get(col) or '').strip()]
        print('empty %-12s :' % col, empt if empt else 'none')

    print('\nstatus distribution:')
    for k, v in Counter((r.get('status') or '').strip() for r in rows).most_common():
        print('  %-14s %d' % (k or '(空)', v))

    ok = (not bad) and (not dupes) and all(
        not (r.get(c) or '').strip() == '' for r in rows for c in ('attempt_date', 'company', 'job_title', 'status')
    )
    print('\n结论:', 'PASS ✅' if ok else 'FAIL ❌')


if __name__ == '__main__':
    main()

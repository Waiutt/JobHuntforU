# -*- coding: utf-8 -*-
"""台账追加工具 —— 从 JSON 追加一行，按「公司+岗位」去重，并同步写 stage_history。

设计要点（都是踩过坑总结出来的）：
  1) `stage_history.csv` 必须是 **7 字段**：date,row_index,company,job_title,from_stage,to_stage,basis
     —— 早期版本只写 3 字段、且把 basis 硬编码成 "submitted via user click"，
        对「撤销 / 被拒」这类行会写出假状态。
  2) 去重命中（未追加）时**不再**写 stage_history —— 早期版本无条件写，会留下幽灵记录。
  3) 台账编码固定 **UTF-8 BOM + CRLF**（Excel 直接双击不乱码），读写一律 `utf-8-sig`。
  4) 每次写入前自动备份为 `_bak_application_log_<时间戳>.csv`。

用法:
    python scripts/ledger_append.py row.json

row.json 最少需要: attempt_date, company, job_title, status
可选覆盖:
    "_from_stage": ""       # 状态流转时的原状态；新投递留空
    "_stage_basis": "…"     # 不填则回退到 submission_evidence 前 400 字
"""
import csv, io, json, os, shutil, sys, datetime

sys.stdout.reconfigure(encoding='utf-8')

DASH = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(DASH, 'application_log.csv')
HIST = os.path.join(DASH, 'stage_history.csv')

HDR = ['attempt_date', 'company', 'job_title', 'job_url', 'platform', 'status',
       'submission_evidence', 'resume_used', 'answers_used', 'confirmation_url',
       'confirmation_text', 'job_description', 'notes', 'stage_detail',
       'outcome_reason', 'stage_updated_at']
HIST_HDR = ['date', 'row_index', 'company', 'job_title', 'from_stage', 'to_stage', 'basis']


def main():
    if len(sys.argv) < 2:
        raise SystemExit('用法: python scripts/ledger_append.py <row.json>')
    src = sys.argv[1]

    if not os.path.exists(LOG):
        raise SystemExit('❌ 找不到台账 %s（请先从 templates/dashboard-template/ 复制一份）' % LOG)

    raw = open(LOG, 'rb').read()
    bom = raw.startswith(b'\xef\xbb\xbf')
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    print('原台账 %d 行' % len(rows))

    new = json.load(io.open(src, encoding='utf-8'))

    # stage_history 表头校验：不符就别写，避免污染
    hraw = open(HIST, 'rb').read() if os.path.exists(HIST) else b''
    hbom = hraw.startswith(b'\xef\xbb\xbf')
    hrows = [r for r in csv.reader(io.StringIO(hraw.decode('utf-8-sig'))) if any(x.strip() for x in r)] if hraw else [HIST_HDR]
    if hrows and hrows[0] != HIST_HDR:
        raise SystemExit('❌ stage_history.csv 表头不符，期望 %s，实际 %s' % (HIST_HDR, hrows[0]))

    key = (new['company'], new['job_title'])
    appended = False
    if key in {(r['company'], r['job_title']) for r in rows}:
        print('  已存在同公司同岗位，跳过')
    else:
        rows.append(new)
        appended = True
        print('  已追加:', new['job_title'][:44])

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    shutil.copy2(LOG, os.path.join(DASH, '_bak_application_log_%s.csv' % stamp))

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HDR, lineterminator='\r\n')
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, '') for k in HDR})
    open(LOG, 'w', encoding=('utf-8-sig' if bom else 'utf-8'), newline='').write(buf.getvalue())
    print('[台账] 写入 %d 行' % len(rows))

    if not appended:
        print('[stage_history] 跳过（未追加新行，不留幽灵记录）')
        return

    basis = (new.get('_stage_basis') or new.get('submission_evidence') or '')[:400]
    hrows.append([
        new['attempt_date'], str(len(rows)), new['company'], new['job_title'],
        new.get('_from_stage', ''), new.get('status', ''), basis,
    ])
    hbuf = io.StringIO()
    csv.writer(hbuf, lineterminator='\r\n').writerows(hrows)
    open(HIST, 'w', encoding=('utf-8-sig' if hbom else 'utf-8'), newline='').write(hbuf.getvalue())
    print('[stage_history] +1 行（7 字段）row_index=%d' % len(rows))


if __name__ == '__main__':
    main()

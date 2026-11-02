# -*- coding: utf-8 -*-
"""
resume-scorer.py — 简历×岗位 匹配度三角色评分器（可复用）

用法:
    python resume-scorer.py <简历文件> <岗位族> [--jd 岗位描述文件] [--debug]

岗位族: ai_pm | data_ops | game_ops | new_media | content_ops | platform_ops | data_analyst | fde

输出:
    HR视角 / 部门成员视角 / 部门负责人视角 三个维度的分项得分、总分(0-100)
    总分 >= 95 判定为「达标,可输出」；否则为「不达标,禁止输出」并给出改进点。

评分逻辑说明(三角色):
    HR视角(35%)   — 筛选漏斗:基础信息完整度、经历数量、关键词覆盖率、单页格式
    部门成员视角(35%) — 干活的人:岗位技能词命中率、量化成果密度、工具链匹配、经历深度
    部门负责人视角(30%) — 业务结果:成果导向、独立主导性、方法论沉淀、自我评价契合度
"""
import sys
import re
import os

# ============ 岗位族关键词库(可扩展) ============
ROLE_KEYWORDS = {
    "ai_pm": {
        "hard": ["产品", "需求", "用户", "竞品", "AI", "模型", "数据分析", "迭代", "原型", "调研"],
        "soft": ["PRD", "Agent", "大模型", "Prompt", "规划", "优先级", "闭环", "指标", "交付"],
        "tools": ["Figma", "Python", "Claude", "DeepSeek", "Vibe Coding"],
    },
    "data_ops": {
        "hard": ["数据", "模型", "分析", "指标", "准确率", "清洗", "自动化", "报表", "Python", "Excel"],
        "soft": ["特征工程", "标注", "画像", "关联", "聚类", "看板", "效率", "订单", "用户"],
        "tools": ["Python", "Excel", "SQL", "CSV", "DeepSeek"],
    },
    "game_ops": {
        "hard": ["游戏", "运营", "玩家", "用户", "活动", "版本", "社区", "数据", "内容"],
        "soft": ["安全", "反作弊", "风控", "违规", "陪练", "考核", "品类", "生态", "增长"],
        "tools": ["Python", "Excel", "剪映", "Figma"],
    },
    "new_media": {
        "hard": ["视频", "内容", "矩阵", "账号", "粉丝", "增长", "选题", "脚本", "数据", "运营"],
        "soft": ["A/B", "复盘", "转化", "曝光", "播放", "剪辑", "文案", "热点", "用户"],
        "tools": ["剪映", "达芬奇", "PS", "AI绘画", "AI文案"],
    },
    "content_ops": {
        "hard": ["内容", "用户", "社区", "活动", "运营", "数据", "分析", "策划", "文案", "增长"],
        "soft": ["UGC", "PGC", "热点", "互动", "留存", "复盘", "选题", "粉丝"],
        "tools": ["剪映", "Figma", "Python"],
    },
    "platform_ops": {
        "hard": ["平台", "运营", "用户", "商家", "数据", "规则", "治理", "策略", "分析", "增长"],
        "soft": ["生态", "审核", "风控", "违规", "标准化", "流程", "画像", "闭环", "效率"],
        "tools": ["Python", "Excel", "SQL"],
    },
    "data_analyst": {
        "hard": ["数据", "分析", "模型", "SQL", "Python", "指标", "可视化", "报表", "清洗", "统计"],
        "soft": ["AB测试", "回归", "画像", "漏斗", "特征", "洞察", "结论", "决策"],
        "tools": ["Python", "Excel", "SQL", "CSV"],
    },
    # FDE / 前线部署 / AI 落地交付族（2026-10-01 新增）
    # 关键词全部取自真实 JD 原文：
    #   语核科技 FDE——「客户业务需求分析、方案设计及 Agent 应用构建实施」「低代码/无代码方式实现
    #     智能体工作流搭建」「客户需求沟通、方案演示及技术培训」「实施文档编写与知识沉淀，包括实施
    #     方案、操作手册及问题排查指南」「能适应阶段性出差」
    #   燧原科技 FDE——「懂代码、懂 AI 能力、懂业务场景、能快速落地迭代」
    #   优艾智合——「项目交付中的机器人软硬件部署调试」「搭建技术支持文档知识库，输出并维护 SOP&FAQ」
    #   星环科技 AI交付——「对客户的技术服务…落地实施、POC 原型验证、技术性售前」
    # ⚠️ hard 的前 5 项决定 lead_score 的「自评契合」——它们必须在正文末尾（自我评价/求职说明）出现。
    "fde": {
        "hard": ["客户", "需求", "交付", "实施", "部署", "方案", "智能体", "工作流", "落地", "迭代"],
        "soft": ["SOP", "文档", "培训", "排障", "标准化", "手册", "闭环", "沉淀", "出差"],
        "tools": ["Python", "SQL", "Claude", "Dify", "Prompt"],
    },
}

# 每个角色的分项权重与检查项定义
def _count(text, words):
    return sum(1 for w in words if w.lower() in text.lower())

def _count_mentions(text, words):
    return sum(text.lower().count(w.lower()) for w in words)

def hr_score(text, role):
    """HR视角:筛选漏斗"""
    checks = {}
    # 1. 基础信息完整度
    checks["联系方式"] = 20 if (("188" in text and "@" in text)) else (10 if ("@" in text or "188" in text) else 0)
    checks["教育背景"] = 15 if ("大学" in text or "本科" in text or "学院" in text) else 0
    # 2. 经历数量(实习>=2段 + 项目>=2个)
    intern_count = len(re.findall(r"(?:至今|20\d\d\.\d\d)", text))
    checks["实习经历"] = 15 if intern_count >= 3 else (10 if intern_count >= 2 else 0)
    proj_markers = sum(text.count(m) for m in ["项目", "系统", "应用", "工作流"])
    checks["项目经历"] = 15 if proj_markers >= 3 else (10 if proj_markers >= 2 else 0)
    # 3. 关键词覆盖率(硬词)
    hard = ROLE_KEYWORDS[role]["hard"]
    hit = _count(text, hard)
    rate = hit / len(hard)
    checks["关键词覆盖率"] = round(20 * rate)
    # 4. 证书/语言/技能板块存在
    has_skill = any(k in text for k in ["技能", "CET", "英语", "证书"])
    checks["技能与证书"] = 15 if has_skill else 0
    total = sum(checks.values())
    return total, checks

def member_score(text, role):
    """部门成员视角:干活的人"""
    checks = {}
    hard = ROLE_KEYWORDS[role]["hard"]
    soft = ROLE_KEYWORDS[role]["soft"]
    tools = ROLE_KEYWORDS[role]["tools"]
    # 1. 硬技能词命中率
    hit = _count(text, hard)
    checks["核心技能"] = round(30 * hit / len(hard))
    # 2. 加分技能(soft)命中
    soft_hit = _count(text, soft)
    checks["进阶能力"] = min(20, soft_hit * 3)
    # 3. 工具链命中(按比例)
    tool_hit = _count(text, tools)
    checks["工具链"] = round(15 * tool_hit / max(1, len(tools)))
    # 4. 量化成果密度(数字出现次数)
    nums = len(re.findall(r"\d+(?:\.\d+)?%?|\d+[万亿]|[0-9,]+", text))
    checks["量化密度"] = min(20, nums // 3)
    # 5. 经历深度(含量化数字的bullet占比 —— 每条bullet都有事实/数字支撑)
    bullets = re.findall(r"▪\s*", text)
    if bullets:
        # 找到每个bullet的内容片段,统计含数字的bullet比例
        parts = re.split(r"▪\s*", text)
        content_parts = [p.strip() for p in parts if p.strip()]
        with_num = sum(1 for p in content_parts if re.search(r"\d", p))
        depth_rate = with_num / len(content_parts)
        checks["经历深度"] = round(15 * depth_rate)
    else:
        checks["经历深度"] = 0
    total = sum(checks.values())
    return total, checks

def lead_score(text, role):
    """部门负责人视角:业务结果"""
    checks = {}
    # 1. 成果导向:包含"提升/增长/覆盖/准确率/效率/识别"等结果词
    result_words = ["提升", "增长", "覆盖", "准确率", "效率", "识别", "爆发", "闭环", "降低", "净增"]
    r_hit = sum(text.count(w) for w in result_words)
    checks["成果导向"] = min(30, r_hit * 3)
    # 2. 独立主导性
    lead_words = ["独立", "主导", "搭建", "负责", "从0到1", "0→1", "0到1", "重构"]
    l_hit = sum(text.count(w) for w in lead_words)
    checks["独立主导"] = min(25, l_hit * 4)
    # 3. 方法论沉淀
    method_words = ["体系", "闭环", "方法论", "框架", "规范", "模型", "沉淀", "流程"]
    m_hit = sum(text.count(w) for w in method_words)
    checks["方法论"] = min(25, m_hit * 3)
    # 4. 自我评价契合度(岗位关键词在自我评价出现,按比例)
    summary = text[-400:]
    s_hit = _count(summary, ROLE_KEYWORDS[role]["hard"][:5])
    checks["自评契合"] = round(20 * s_hit / 5)
    total = sum(checks.values())
    return total, checks

def score_resume(text, role):
    hr_t, hr_c = hr_score(text, role)
    me_t, me_c = member_score(text, role)
    ld_t, ld_c = lead_score(text, role)
    total = round(0.35 * hr_t + 0.35 * me_t + 0.30 * ld_t, 1)
    return {
        "hr": (hr_t, hr_c),
        "member": (me_t, me_c),
        "lead": (ld_t, ld_c),
        "total": total,
    }

def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".pdf",):
        try:
            import fitz
            doc = fitz.open(path)
            # ⚠️ 必须用 sort=True（自然阅读序）：
            # PyMuPDF 默认按 PDF 内部块存储顺序返回文本，而本仓库的简历模板
            # （.page 固定 297mm + 绝对/弹性布局）其块序 ≠ 视觉阅读序 ——
            # 实测同一份 PDF：默认序读到的"正文末尾"是「实习/项目」条目，
            # 而视觉末尾其实是「自我评价 / 求职说明」。
            # 而 lead_score 的「自评契合」只在**正文末尾 400 字**里找岗位关键词，
            # 于是永远读错段落 → 同一份简历默认序 91.3 FAIL、阅读序 96.5 PASS。
            # 门禁评估的是"人从上到下读到的内容"，故取阅读序为唯一口径。
            return "\n".join(p.get_text(sort=True) for p in doc)
        except ImportError:
            print("需要 PyMuPDF: pip install pymupdf")
            sys.exit(1)
    elif ext in (".html", ".htm"):
        with open(path, encoding="utf-8") as f:
            html = f.read()
        text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return text
    elif ext in (".md", ".txt"):
        with open(path, encoding="utf-8") as f:
            return f.read()
    else:
        print("不支持的格式:", ext)
        sys.exit(1)

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    resume_path = sys.argv[1]
    role = sys.argv[2]
    debug = "--debug" in sys.argv
    if role not in ROLE_KEYWORDS:
        print("岗位族必须是:", ", ".join(ROLE_KEYWORDS.keys()))
        sys.exit(1)
    text = extract_text(resume_path)
    result = score_resume(text, role)
    print("=" * 56)
    print("简历:", os.path.basename(resume_path))
    print("岗位族:", role)
    print("-" * 56)
    names = {"hr": "HR视角(35%)", "member": "部门成员视角(35%)", "lead": "部门负责人视角(30%)"}
    for k, v in result.items():
        if k == "total":
            continue
        score, checks = v
        print(f"{names[k]:<16} {score:>3}/100")
        if debug:
            for ck, cv in checks.items():
                print(f"    - {ck}: {cv}")
    print("-" * 56)
    total = result["total"]
    status = "PASS (>=95), 可以输出" if total >= 95 else f"FAIL ({total}<95), 禁止输出"
    print(f"{'综合匹配度':<14} {total:>5}/100  {status}")
    print("=" * 56)
    return 0 if total >= 95 else 2

if __name__ == "__main__":
    sys.exit(main())

"""全部阈值集中在这里。

写成一个文件不是为了好看。评测报告里三项指标要站得住，前提是判定规则
在跑数据之前就已经固定下来，而不是测完再回头凑。所以：
  改这个文件要在 git 里留一次独立提交，提交时间必须早于评测批次的时间戳。
答辩被问到"这个 85% 怎么来的"，直接翻这个文件加提交记录。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
KB_PATH = DATA / "kb" / "robotics.jsonl"
KP_PATH = DATA / "knowledge_points.json"
PRETEST_PATH = DATA / "pretest.json"
PROFILE_DIR = DATA / "profiles"

# ---- 学情诊断 ----
# 掌握度由 BKT（贝叶斯知识追踪）估计，输出为掌握概率。分档阈值：
MASTERY_BLIND = 0.25      # 低于此值判为盲区（约等于四选一的蒙对基线）
MASTERY_WEAK = 0.50       # 半数以下，判为薄弱
MASTERY_OK = 0.80         # 八成以上判为掌握
UNTESTED_AS_GAP = True    # 前测未覆盖的知识点按盲区处理（保守策略）

# BKT 参数。现为文献常用初值，拿到真实作答数据后用 evalkit/fit_bkt.py 重新拟合。
# 约束 p_S + p_G < 1 必须满足，否则模型退化，见 core/bkt.py 的说明。
BKT_P_T = 0.15
BKT_P_S = 0.10
BKT_P_G = 0.25            # 四选一客观题的理论蒙对率
BKT_USE_PRIOR = True      # 用学历与实操学时给初始掌握概率先验

# ---- 现场命题 ----
# 命题审核比内容审核更严：题目答案错了，等于拿错尺子量人，
# 而且整条链路没有任何环节会发现。宁可用糙题，不能用错题。
EXAMINER_ENABLED = True     # 关闭后只用固定题库，用于消融对照
ITEM_ANSWER_MIN = 0.55      # 正确答案与所引切片的证据覆盖率下限
ITEM_DISTRACTOR_MAX = 0.50  # 干扰项覆盖率上限，超过视为可能同样成立
ITEM_MAX_GENERATED = 8      # 单次测评最多现场出几道，防止无限生成
# 生成题的失误率取更保守的值：实际区分度未知，等价于给它的信息量打折
BKT_P_S_GENERATED = 0.18

# ---- 交叉验证与辩论 ----
DEBATE_ENABLED = True     # 关闭后退化为单专家生成，用于消融对照
DEBATE_ALIGN_MIN = 0.55   # 两位专家的断言相似度达到此值视为在讲同一件事
DEBATE_ALIGN_MIN_SAME_SRC = 0.35  # 引用同一切片时的放宽阈值，见 agents/debate.py
DEBATE_CONFLICT_MARGIN = 0.12  # 仲裁时证据分差需超过此值才判一方胜出
CONSENSUS_BONUS = 0.10    # 双专家一致的断言，证据分加成（仅用于排序展示）


# ---- 资源难度适配 ----
# 适配规则（对应评测指标"画像-资源难度适配准确率"）：
# 目标难度 = 知识点固有难度经掌握度修正后的值，落在 [当前水平, 当前水平+2] 视为适配。
ADAPT_WINDOW_LOW = 0
ADAPT_WINDOW_HIGH = 2
DIFFICULTY_MIN = 1
DIFFICULTY_MAX = 5

# ---- 审核裁判 ----
EVIDENCE_MIN = 0.42       # 断言与所引切片的二元组覆盖率下限
NUMERIC_STRICT = True     # 断言中出现的数字必须在所引切片中出现，否则判冲突
TERM_STRICT = True        # 断言使用的领域特征术语必须出现在所引切片中
TERM_MISS_TOLERANCE = 1   # 允许缺失的特征术语个数，1 表示缺一个就拦
MISATTRIB_MARGIN = 0.15   # 全库最佳切片比所引切片高出这么多，判为引用错位；0 关闭
MAX_REVISE_ROUNDS = 2     # 打回重写的最大轮数，超过则丢弃该断言

# ---- 反馈迭代决策 ----
DECIDE_DOWN = 0.50        # 正确率低于此值 -> 降维解释
DECIDE_ADVANCE = 0.85     # 正确率高于此值 -> 进阶挑战
DECIDE_MIN_ITEMS = 2      # 少于这么多题不做决策，避免样本太小乱跳

# ---- 生成 ----
CLAIMS_PER_KP = 5
RETRIEVE_TOP_K = 3

# ---- 评测目标（来自榜题评分标准） ----
TARGET_HALLUCINATION = 0.05
TARGET_ADAPT = 0.85
TARGET_COVERAGE = 0.90

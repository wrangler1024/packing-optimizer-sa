# 装箱助手-模拟退火-v1

## 这是什么

亚马逊精铺项目的装箱优化工具。输入SKU数据（CSV），输出最优装箱方案（CSV），让每箱运费最低、箱数最少、仓库拣货最快。

## 谁用

精铺项目运营团队。在创建FBA货件前，用它规划装箱方案。

## 核心文件

```
packing-optimizer-sa-v1/
├── README.md              ← 你正在看的这个文件
├── SKILL.md               ← Agent技能定义（ZCode格式）
├── scripts/
│   └── optimizer.py       ← 核心算法脚本（纯Python，零依赖）
└── examples/
    ├── sample_input.csv   ← 示例输入（20款SKU）
    └── sample_output.csv  ← 示例输出（装箱方案）
```

## 怎么用

### 输入

运营从飞书多维表格导出SKU装箱数据的CSV：

```
SKU编号,产品名称,品类,单件重量(kg),包装长(cm),包装宽(cm),包装高(cm),发货数量,备注
JP-0001,硅胶沥水垫,硅胶,0.15,30.0,20.0,2.0,30,需抽真空
JP-0002,不锈钢量勺5件套,金属,0.2,15.0,10.0,3.0,30,
```

### 运行

```bash
python3 scripts/optimizer.py input.csv
```

可选参数：
```bash
python3 scripts/optimizer.py input.csv --max-sku 10        # 每箱最多10款SKU（默认5）
python3 scripts/optimizer.py input.csv --air-price 42      # 空运单价¥42/kg（默认45）
python3 scripts/optimizer.py input.csv --output 方案.csv    # 指定输出路径
```

### 输出

```
packing_result.csv  ← 装箱方案（每箱装了哪些SKU各几件）
packing_result.json ← 完整报告（汇总统计）
```

## 在不同Agent工具中使用

### ZCode

把整个文件夹放到 `~/.zcode/skills/` 目录下：

```bash
cp -r packing-optimizer-sa-v1 ~/.zcode/skills/
```

然后在ZCode对话中说："帮我跑装箱优化"，附上CSV文件即可。

### WorkBuddy

把整个文件夹放到WorkBuddy的技能目录下（具体路径参考WorkBuddy文档），然后在对话中说：

"用 scripts/optimizer.py 帮我优化这个CSV的装箱方案"

### Claude Code / Cursor / 其他AI编程工具

不需要安装到技能目录。直接在对话中说：

"用 packing-optimizer-sa-v1/scripts/optimizer.py 帮我跑装箱优化，输入文件是xxx.csv"

Agent会自动执行 `python3 optimizer.py xxx.csv`。

### 纯命令行（不用Agent工具）

```bash
cd packing-optimizer-sa-v1
python3 scripts/optimizer.py 你的SKU数据.csv
```

## 算法说明

| 步骤 | 策略 | 目标 |
|---|---|---|
| 贪心初始化 | SKU集中装箱（同款货尽量装一起）+ 重轻交替 | 生成初始可行解 |
| 模拟退火 | 随机扰动（移动/交换/合并）+ 概率接受差解 | 搜索更优方案 |
| 箱型优化 | 每箱选计费重量最小的箱型 | 降低运费 |

## 约束参数

| 参数 | 默认值 | 可调 | 说明 |
|---|---|---|---|
| 每箱SKU数 | ≤5款 | `--max-sku` | 仓库拣货约束 |
| 单箱重量 | ≤15kg | `--max-wt` | FBA标准 |
| 空运单价 | ¥45/kg | `--air-price` | 头程运费 |
| 箱型 | 1-5号箱 | 代码内改 | 排除6-7号（体积重太高）|

## 环境要求

- Python 3.8+
- 无第三方依赖（纯标准库，不需要pip install任何东西）

## 版本记录

| 版本 | 日期 | 改进 |
|---|---|---|
| v1.0 | 2026-07-23 | 初始版本：贪心+模拟退火+SKU集中策略 |

# 静态指标软件演示说明

本文是项目自带的软件演示知识，不是航空设计要求、适航标准或专家审核意见。
所有阈值均为测试软件流程而人为设定，不得用于真实飞行器判定。

## 适用范围

仅适用于 aircraft_type=demo_fixed_wing、condition=demo_cruise，
模型版本 demo-static-v1，判据版本 demo-rules-v1。
这不是动态仿真，不涉及稳定性、结构强度或控制系统验证。

## 指标定义

演示使用固定重力加速度 g=9.80665 m/s^2，以 W=mass_kg*g 计算重量。
翼载 wing_loading=W/wing_area_m2，单位 N/m^2。
动压 dynamic_pressure=0.5*air_density_kg_m3*airspeed_mps^2，单位 Pa。
需用升力系数 required_lift_coefficient=W/(dynamic_pressure*wing_area_m2)，无量纲，单位标记为 1。
推重比 thrust_to_weight=thrust_n/W，无量纲，单位标记为 1。
升力系数的算式仅体现静态水平力平衡，不证明真实飞行器能够达到该升力系数。

## 演示判据

DEMO-WL：翼载 wing_loading <= 180 N/m^2。
DEMO-Q：动压 dynamic_pressure >= 100 Pa。
DEMO-CL：需用升力系数 required_lift_coefficient <= 1.2。
DEMO-TW：推重比 thrust_to_weight >= 0.25。
边界值包含在通过范围内，无额外容差；计算使用未格式化的数值，不先四舍五入。
这四个阈值只用于软件演示，不代表任何真实机型或任务的可接受范围。

## 判定解释

pass 表示单项满足上述演示规则；fail 表示至少一项不满足。
undetermined 表示缺少指标、单位不符、模型版本不符或结果无效。
not_applicable 表示机型或工况与条款不匹配，不能替代 pass。
出现 fail 和未判定项时，总状态为 fail，同时仍须披露未判定项及覆盖率。
只有全部适用项都已评估并通过时，总状态才是 pass；它仍不代表飞行安全或适航批准。

## 复核提示

应先核对输入单位、工况、模型假设和输出有效性。
模型生成的原因分析只是待验证假设，不能自动修改参数或判据后宣称问题已解决。

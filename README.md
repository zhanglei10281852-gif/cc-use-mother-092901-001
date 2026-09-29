# 跨区域道路测试许可协同

本项目提供道路测试许可协同的领域契约，描述申请版本、车辆与驾驶员资格、路线时窗以及许可状态。后续服务可在这些稳定对象上实现辖区审查、冲突检测和历史追溯。

运行测试：

\`python -m unittest discover -s tests -v\`

编译检查：

\`python -m compileall -q src tests run_cli.py\`

命令行冒烟：

\`python run_cli.py\`

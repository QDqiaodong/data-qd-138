# data-qd-138 · 栅格目标连通域提取

Bounded binary raster component labeling and object geometry。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/raster-objects --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/raster_objects/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前业务与样例
# Binary raster objects v1

为影像标注整理人员提取二值目标，输入connectivity=4或8与非空矩形grid，宽高<=500，总格<=100000。值为0背景、1目标或null未知，严格整数不接受布尔。所有坐标为0起点行列，不声称真实地理坐标。
行优先扫描赋连通域编号，4/8邻接决定连通性。每目标输出格数、整数边界框、精确分数质心和正交边长计数周长；即使8邻接也始终按4条暴露边计算周长。网格外、背景与未知邻格都视暴露，但null保持未知数，不并入已知背景。输出标签栅格null保留null。
样例3目标，面积3/2/1格、周长8/6/4；共6目标格、5背景格、1未知格。空目标栅格合法。不是地形汇流、矢量多边形叠加、孔洞拓扑或物理面积估计；不读取GeoTIFF。


"""插件登记处。骨架期显式注册；外部插件目录加载是后续插卡点，不是现在的需求。"""
from plugins.detectors.no_consumer import NoConsumerDetector
from plugins.detectors.reachability import ReachabilityDetector
from plugins.detectors.state_health import StateHealthDetector
from plugins.sensors.ast_scan import AstScanSensor
from plugins.sensors.code_scan import CodeScanSensor
from plugins.sensors.doc_scan import DocScanSensor
from plugins.sensors.import_graph import ImportGraphSensor
from plugins.sensors.js_scan import JsScanSensor
from plugins.sensors.rust_scan import RustScanSensor
from plugins.sensors.trace_scan import TraceScanSensor

# Go/TS 表面的视力取决于 tree-sitter 是否可用：在则 AST 级（go_ast_scan/ts_ast_scan，
# grounding 标 structural），不在则退回词法代理（go_scan/js_scan，grounding 如实标
# lexical）。判定依据强度按证据 kind 自曝，降级对用户可见而不是静默丢失（16.4）。
try:
    from plugins.sensors.go_ast_scan import GoAstScanSensor

    _GO_SENSOR = GoAstScanSensor()
except ImportError:
    from plugins.sensors.go_scan import GoScanSensor

    _GO_SENSOR = GoScanSensor()

try:
    from plugins.sensors.ts_ast_scan import TsAstScanSensor

    _JS_SENSOR = TsAstScanSensor()
except ImportError:
    from plugins.sensors.js_scan import JsScanSensor

    _JS_SENSOR = JsScanSensor()

try:
    from plugins.sensors.rust_ast_scan import RustAstScanSensor

    _RUST_SENSOR = RustAstScanSensor()
except ImportError:
    from plugins.sensors.rust_scan import RustScanSensor

    _RUST_SENSOR = RustScanSensor()

SENSORS = [
    CodeScanSensor(), DocScanSensor(), AstScanSensor(),
    _JS_SENSOR, _GO_SENSOR, _RUST_SENSOR, ImportGraphSensor(),
    TraceScanSensor(),
]
DETECTORS = [NoConsumerDetector(), StateHealthDetector(), ReachabilityDetector()]

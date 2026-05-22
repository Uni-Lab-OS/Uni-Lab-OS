# fault_injection e2e 测试用配置
# ak/sk 请替换为本地 Go 后端创建的实验室凭证

class BasicConfig:
    ak = "REPLACE_WITH_LAB_AK"
    sk = "REPLACE_WITH_LAB_SK"
    machine_name = "fault_injection_edge"


class WSConfig:
    reconnect_interval = 5
    max_reconnect_attempts = 999
    ping_interval = 30


class HTTPConfig:
    # 指向本地 Go 后端
    remote_addr = "http://127.0.0.1:48197/api/v1"

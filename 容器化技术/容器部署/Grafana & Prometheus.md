```yaml
version: '3.8'

services:
  prometheus:
    image: prom/prometheus:v2.52.0
    container_name: prometheus
    restart: always
    ports:
      - "9090:9090"
    volumes:
      # 仅挂载配置文件
      - ./prometheus/prometheus.yaml:/etc/prometheus/prometheus.yml
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--web.enable-lifecycle' # 支持热重载 curl -X POST http://127.0.0.1:9090/-/reload
    networks:
      - monitor-net

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    restart: always
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=Admin@123456
    networks:
      - monitor-net
    depends_on:
      - prometheus

networks:
  monitor-net:


```

### prometheus/prometheus.yaml
```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  # - "alert.rules.yml"

scrape_configs:
  # 抓取prometheus自身指标
  - job_name: 'prometheus'
    static_configs:
      - targets: ['prometheus:9090']

  # SpringBoot应用JVM指标（actuator prometheus端点）
  - job_name: 'springboot-jvm'
    metrics_path: '/actuator/prometheus'
    static_configs:
      - targets: ['192.168.6.207:8080'] # 修改为你的SpringBoot服务地址
```
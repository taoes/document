
```shell
docker run --name nacos-standalone 
 -e MODE=standalone \
 -e NACOS_AUTH_TOKEN=+uIShtsHBfxbt1CF8c0NixNaZcmopLVzayCyFx72+reVlQVKxKNqlS/fxyNU7B3b \
 -e NACOS_AUTH_IDENTITY_KEY=your_identity_key \
 -e NACOS_AUTH_IDENTITY_VALUE=+uIShtsHBfxbt1CF8c0NixNaZcmopLVzayCyFx72+reVlQVKxKNqlS/fxyNU7B3b \
 -p 8080:8080 \
 -p 8848:8848 \
 -p 9848:9848 \
 -d nacos/nacos-server:latest
```
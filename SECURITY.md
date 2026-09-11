# 安全说明

## 部署边界

本服务面向单个用户或受信任的家庭网络。通过可信局域网、VPN 或 HTTPS 反向代理访问；不要在不可信网络中通过明文 HTTP 输入服务密钥、Telegram 验证码或两步验证密码。

服务只公开首页和健康检查。其他请求必须提供服务访问密钥；权限判断使用服务器的原始 ASGI 请求路径，不使用由 Host 头拼接出的 URL。

`data/` 中的 Telegram 会话等同账号登录凭证。请保持应用数据和备份私密，不要把它们放入公开仓库、公开网盘或下载目录。服务只应挂载需要保存视频的文件夹。

## 更新与验证

更新代码后运行 `bash install.sh`，重新构建并创建容器。只更新仓库文件或重启旧容器，不会替换旧镜像中的代码和依赖。

回归测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

依赖审计（需访问漏洞数据库）：

```bash
.venv/bin/pip install pip-audit
.venv/bin/pip-audit -r requirements.txt
```

扫描结果仅反映扫描时已收录的漏洞，不等同于全面安全保证。遇到问题时，请不要在公开 Issue 中上传访问密钥、会话数据库或真实账号凭证。

#!/data/data/com.termux/files/usr/bin/bash
# linux.sb 自动回帖机器人 —— Termux(Android)启动脚本
#
# 用法:
#   bash start.sh              前台运行,Ctrl+C 停止
#   tmux new -s bot            建议:先进 tmux 再运行,可随时 detach 挂后台
#
# 首次部署:
#   pkg update && pkg install python nodejs tmux
#   (若从 Windows 拷过来,先修正换行符:
#    sed -i 's/\r$//' start.sh bot.py $(find lzsb_bot -name '*.py'))

cd "$(dirname "$0")" || exit 1

if ! command -v python >/dev/null 2>&1; then
  echo "[错误] 没有找到 python。请先执行: pkg install python"
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "[警告] 没有找到 node —— Cap 人机验证求解需要它,否则无法登录。"
  echo "       请执行: pkg install nodejs"
  echo
fi

if [ ! -f config.json ]; then
  echo "[提示] 还没有 config.json,正在从 config.example.json 复制……"
  cp config.example.json config.json
  echo "       请编辑 config.json,填好账号密码后再运行。"
  exit 1
fi

if [ ! -f llm.json ]; then
  echo "[提示] 还没有 llm.json,正在从 llm.example.json 复制……"
  cp llm.example.json llm.json
  echo "       请编辑 llm.json,填好 AI 接口(base_url / api_key / model)后再运行。"
  exit 1
fi

# 手机熄屏后系统会冻结后台进程,拿唤醒锁可避免跑到一半被杀
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  echo "[提示] 已获取唤醒锁,避免熄屏后进程被冻结"
fi

echo "=================================================="
echo "  linux.sb 自动回帖机器人 (Termux)"
echo "  配置文件: config.json(站点/搜索) + llm.json(AI 接口)"
echo "  提醒: Clash 代理必须保持开启"
echo "  按 Ctrl+C 可随时停止"
echo "=================================================="
echo

python -m lzsb_bot

if command -v termux-wake-unlock >/dev/null 2>&1; then
  termux-wake-unlock
fi

echo
echo "机器人已退出。"

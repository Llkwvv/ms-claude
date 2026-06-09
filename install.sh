#!/bin/bash
# ms-claude 一键安装/部署脚本
# 支持两种部署方式：本地 Python / Docker
# 用法: ./install.sh [local|docker]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_NAME="ms-claude"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERR ]${NC} $*" >&2; }

# ================================
# 通用检查
# ================================
check_prerequisites() {
    info "检查基础环境..."
    if ! command -v git &>/dev/null; then
        error "请先安装 git"
        exit 1
    fi
    if ! command -v python3 &>/dev/null; then
        error "请先安装 Python 3.9+"
        exit 1
    fi
    info "环境检查通过"
}

# ================================
# 方式一：本地 Python 部署
# ================================
install_local() {
    info "========== 本地 Python 部署 =========="

    # 1. 创建隔离目录
    read -rp "请输入安装目录 [默认: ~/.local/share/ms-claude]: " INSTALL_DIR
    INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/ms-claude}"
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    info "安装目录: $INSTALL_DIR"

    # 2. 克隆或更新代码
    if [ -d "$REPO_NAME/.git" ]; then
        info "检测到已存在的仓库，执行更新..."
        cd "$REPO_NAME"
        git pull origin main
    else
        info "克隆仓库..."
        git clone https://github.com/Llkwvv/ms-claude.git "$REPO_NAME"
        cd "$REPO_NAME"
    fi

    # 3. 创建虚拟环境
    VENV_DIR="$INSTALL_DIR/$REPO_NAME/.venv"
    if [ ! -d "$VENV_DIR" ]; then
        info "创建 Python 虚拟环境..."
        python3 -m venv "$VENV_DIR"
    fi
    source "$VENV_DIR/bin/activate"

    # 4. 安装依赖
    info "安装 Python 依赖..."
    pip install --upgrade pip
    pip install -r requirements.txt

    # 5. 配置环境变量
    ENV_FILE="$INSTALL_DIR/$REPO_NAME/.env"
    if [ ! -f "$ENV_FILE" ]; then
        info "创建 .env 配置文件..."
        cp .env.example .env
        warn "请编辑 $ENV_FILE 填入你的上游 API Key"
    else
        info ".env 已存在，跳过创建"
    fi

    # 6. 创建 systemd 服务文件（可选）
    if command -v systemctl &>/dev/null; then
        read -rp "是否创建 systemd 服务? [y/N]: " CREATE_SERVICE
        if [[ "$CREATE_SERVICE" =~ ^[Yy]$ ]]; then
            SERVICE_FILE="$HOME/.config/systemd/user/ms-claude.service"
            mkdir -p "$(dirname "$SERVICE_FILE")"
            cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=ms-claude Model Proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/$REPO_NAME
Environment=MS_CLAUDE_HOME=$INSTALL_DIR/$REPO_NAME
EnvironmentFile=$INSTALL_DIR/$REPO_NAME/.env
ExecStart=$VENV_DIR/bin/python3 -m src.main --serve --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
            systemctl --user daemon-reload
            info "systemd 服务已创建: $SERVICE_FILE"
            info "启动: systemctl --user start ms-claude"
            info "开机自启: systemctl --user enable ms-claude"
        fi
    fi

    # 7. 创建快捷命令
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
    cat > "$BIN_DIR/ms-claude" <<EOF
#!/bin/bash
source "$VENV_DIR/bin/activate"
cd "$INSTALL_DIR/$REPO_NAME"
exec python3 -m src.main "\$@"
EOF
    chmod +x "$BIN_DIR/ms-claude"

    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        warn "$BIN_DIR 不在 PATH 中，请添加以下行到 ~/.bashrc 或 ~/.zshrc:"
        warn "export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi

    info ""
    info "========== 本地部署完成 =========="
    info "安装目录: $INSTALL_DIR/$REPO_NAME"
    info "配置文件: $ENV_FILE"
    info "启动代理: ms-claude --serve"
    info "查看状态: ms-claude --status"
    info "运行测试: make test"
    info ""
    warn "注意：请务必编辑 .env 填入你的 API Key 后再启动！"
}

# ================================
# 方式二：Docker 部署
# ================================
install_docker() {
    info "========== Docker 部署 =========="

    if ! command -v docker &>/dev/null; then
        error "请先安装 Docker: https://docs.docker.com/get-docker/"
        exit 1
    fi
    if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null; then
        error "请先安装 docker-compose"
        exit 1
    fi

    # 1. 创建部署目录
    read -rp "请输入部署目录 [默认: /opt/ms-claude]: " DEPLOY_DIR
    DEPLOY_DIR="${DEPLOY_DIR:-/opt/ms-claude}"
    mkdir -p "$DEPLOY_DIR"
    cd "$DEPLOY_DIR"
    info "部署目录: $DEPLOY_DIR"

    # 2. 克隆或更新代码
    if [ -d "$REPO_NAME/.git" ]; then
        info "检测到已存在的仓库，执行更新..."
        cd "$REPO_NAME"
        git pull origin main
    else
        info "克隆仓库..."
        git clone https://github.com/Llkwvv/ms-claude.git "$REPO_NAME"
        cd "$REPO_NAME"
    fi

    # 3. 创建 .env
    if [ ! -f .env ]; then
        info "创建 .env 配置文件..."
        cp .env.example .env
        warn "请编辑 $DEPLOY_DIR/$REPO_NAME/.env 填入你的上游 API Key"
    fi

    # 4. 创建数据/日志目录
    mkdir -p data logs

    # 5. 构建并启动
    info "构建 Docker 镜像..."
    if docker compose version &>/dev/null 2>&1; then
        docker compose build
    else
        docker-compose build
    fi

    info ""
    info "========== Docker 部署完成 =========="
    info "部署目录: $DEPLOY_DIR/$REPO_NAME"
    info "配置文件: $DEPLOY_DIR/$REPO_NAME/.env"
    info ""
    warn "请按以下步骤操作："
    warn "  1. 编辑 .env 文件，填入 MS_CLAUDE_UPSTREAM_API_KEY"
    warn "  2. 启动服务: docker compose up -d"
    warn "  3. 查看日志: docker compose logs -f"
    warn "  4. 停止服务: docker compose down"
    info ""
    info "代理将监听 0.0.0.0:8080"
}

# ================================
# 主逻辑
# ================================
main() {
    MODE="${1:-}"

    # 如果没有参数，交互式选择
    if [ -z "$MODE" ]; then
        echo "请选择部署方式:"
        echo "  1) 本地 Python 部署 (推荐个人开发)"
        echo "  2) Docker 部署 (推荐服务器)"
        read -rp "输入选项 [1/2]: " CHOICE
        case "$CHOICE" in
            1) MODE="local" ;;
            2) MODE="docker" ;;
            *) error "无效选项"; exit 1 ;;
        esac
    fi

    check_prerequisites

    case "$MODE" in
        local)
            install_local
            ;;
        docker)
            install_docker
            ;;
        *)
            echo "用法: $0 [local|docker]"
            echo ""
            echo "  local  - 本地 Python 虚拟环境部署（含 systemd 服务可选）"
            echo "  docker - Docker Compose 容器化部署"
            exit 1
            ;;
    esac
}

main "$@"

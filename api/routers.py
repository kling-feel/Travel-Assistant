from fastapi import APIRouter, FastAPI

from config import settings


def router_v1():
    # 主路由
    root_router = APIRouter()
    # 旧路由仍使用客户端提供的旅客/会话 ID 和未绑定的写操作。
    # P1/P2 完成授权及逐笔确认之前，不对外开放它们。
    if settings.ENABLE_LEGACY_API:
        from api.system_mgt import user_views
        from api.graph_api import graph_views

        root_router.include_router(user_views.router, tags=['用户管理（旧版）'])
        root_router.include_router(graph_views.router, tags=['工作流调用（旧版）'])

    return root_router


def init_routers(app: FastAPI):
    app.include_router(router_v1(), prefix='/api')

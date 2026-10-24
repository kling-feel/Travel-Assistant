from fastapi import APIRouter, FastAPI

def router_v1():
    # 主路由
    root_router = APIRouter()
    from travel_assistant.api.travel import router as travel_router
    root_router.include_router(travel_router, prefix='/v1', tags=['旅客业务'])
    return root_router


def init_routers(app: FastAPI):
    app.include_router(router_v1(), prefix='/api')

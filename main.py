import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles
from travel_assistant.core.config import settings
from travel_assistant.core import handler_error, cors

from travel_assistant.core.log_config import init_log
from travel_assistant.api import routers
from travel_assistant.paths import STATIC_DIR


class Server:

    def __init__(self):
        init_log()  # 加载日志的配置
        self.app = FastAPI()
        self.app.add_api_route('/health', lambda: {'status': 'ok'}, methods=['GET'], include_in_schema=False)
        self.app.add_api_route('/app', self.traveler_page, methods=['GET'], include_in_schema=False)
        self.app.mount('/static', StaticFiles(directory=STATIC_DIR), name='my_static')

    @staticmethod
    def traveler_page():
        return FileResponse(
            STATIC_DIR / 'app.html',
            headers={
                'Cache-Control': 'no-store',
                'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer',
            },
        )

    def init_app(self):
        # 初始化全局异常处理
        handler_error.init_handler_errors(self.app)
        # 初始化全局CORS跨域的处理
        cors.init_cors(self.app)
        # 初始化主路由
        routers.init_routers(self.app)

    def run(self):
        self.init_app()
        uvicorn.run(
            app=self.app,
            host=settings.HOST,
            port=settings.PORT
        )


if __name__ == '__main__':
    Server().run()

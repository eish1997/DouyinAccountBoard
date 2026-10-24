"""Persistent internal website; all clients use the existing canonical database."""
import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))


def configuration(path,environment=None):
    environment=os.environ if environment is None else environment
    config=json.loads(Path(path).read_text(encoding='utf-8'))
    if config.get('enabled') is not True:raise ValueError('网站服务未启用')
    if environment.get('PORT'):
        if not environment['PORT'].isascii() or not environment['PORT'].isdigit():raise ValueError('平台PORT无效')
        config['port']=int(environment['PORT'])
    if config.get('host') not in ['127.0.0.1','0.0.0.0']:raise ValueError('网站监听地址无效')
    backend=config.get('backend','sqlite')
    if backend=='cloudbase':
        if config.get('host')!='0.0.0.0':raise ValueError('CloudBase 模式必须监听 0.0.0.0')
        if not environment.get('PORT'):raise ValueError('CloudBase 模式必须使用平台 PORT')
        if config.get('data_dir') or config.get('database'):raise ValueError('CloudBase 模式不使用 SQLite 数据目录或本机合成库')
        if not isinstance(config.get('port'),int) or not 1<=config['port']<=65535:raise ValueError('平台PORT无效')
        return config
    if not isinstance(config.get('port'),int) or not 1024<=config['port']<=65535:raise ValueError('网站端口无效')
    if backend=='postgresql':
        from test_isolation import checked_root
        if config['host']!='127.0.0.1' or config.get('database',{}).get('synthetic_root')!=str(checked_root()):
            raise ValueError('PostgreSQL候选入口目前仅允许明确的本机合成环境')
        return config
    if backend!='sqlite':raise ValueError('数据库后端无效')
    data_dir=Path(config['data_dir']).resolve()
    if data_dir!=ROOT/'data':raise ValueError('共享服务必须使用现有正式数据目录')
    if not (data_dir/'board.db').is_file():raise ValueError('正式数据库不存在，停止启动以免创建空库')
    return config


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=configuration(args.config)
    postgres=config.get('backend')=='postgresql'
    cloudbase=config.get('backend')=='cloudbase'
    if postgres:
        from test_isolation import checked_root,install
        install(checked_root())
    elif not cloudbase:
        os.environ['DOUYIN_BOARD_DATA_DIR']=config['data_dir']
    os.environ['DOUYIN_BOARD_WORKER_TOKEN']=''
    os.environ['DOUYIN_BOARD_SHARED_WEB']='1'
    if cloudbase:
        if config.get('public_origin'):os.environ['DOUYIN_BOARD_PUBLIC_ORIGIN']=config['public_origin']
    else:
        os.environ['DOUYIN_BOARD_PUBLIC_ORIGIN']=config.get('public_origin','')
    if postgres or cloudbase:
        handler=logging.StreamHandler(sys.stdout)
    else:
        logs=ROOT/'logs';logs.mkdir(exist_ok=True)
        handler=RotatingFileHandler(logs/'website.log',maxBytes=5*1024*1024,backupCount=4,encoding='utf-8')
    logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
    import app
    if cloudbase:
        from cloudbase_runtime import CloudBaseRuntime
        app.configure_database(CloudBaseRuntime(os.environ))
    elif postgres:
        from pg_runtime import PostgresRuntime
        app.configure_database(PostgresRuntime(config['database'],os.environ))
    app.initialize()
    if not cloudbase:
        with app.connect() as db:
            if not app.auth.initialized(db):raise ValueError('请先在本机桌面软件创建总管理员，再启用共享网站')
    if not postgres and not cloudbase:app.backup.start(app.DB)
    class WebHandler(app.Handler):
        def log_message(self,format,*args):logging.info('%s %s',self.client_address[0],format%args)
    app.ThreadingHTTPServer.request_queue_size=app.REQUEST_QUEUE_LIMIT
    server=app.ThreadingHTTPServer((config['host'],config['port']),WebHandler)
    logging.info('Website listening at %s:%s',config['host'],config['port'])
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()

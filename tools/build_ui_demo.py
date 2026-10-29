"""Build a separate, conspicuously marked, network-free DevTools UI demo.

Only copies tracked mini-program source plus explicit offline fixtures. Never
imports production credentials, database, or uploads. Output must be NEW and
outside the repository. Production privacy approval remains empty.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import zlib

ROOT=Path(__file__).resolve().parents[1]


def png(path,index):
    # Code-drawn placeholder product illustration, not real merchandise imagery.
    width=height=320
    bg=[(232,238,225),(230,235,233),(249,236,215)][index % 3]
    ink=[(234,222,201),(101,139,123),(211,144,77)][index % 3]
    data=bytearray()
    for y in range(height):
        data.append(0)
        for x in range(width):
            draw=(90<x<220 and 86<y<250) if index!=1 else (60<x<260 and 118<y<230)
            if index==0:draw=draw or (210<x<258 and 120<y<190 and ((x-228)**2+(y-155)**2<900))
            if index==3:draw=((x-145)**2+(y-185)**2<65**2 or (x-215)**2+(y-180)**2<48**2 or (125<x<175 and 85<y<125))
            if index==4:draw=(75<x<245 and 140<y<265) or (100<x<220 and 75<y<150 and not (115<x<205 and 92<y<140))
            if index==5:draw=(90<x<230 and 70<y<255) and not ((x-160)**2+(y-165)**2<42**2)
            if index==6:draw=(110<x<210 and 130<y<265) or (135<x<185 and 85<y<135) or (150<x<225 and 73<y<93)
            if index==7:draw=(75<x<245 and 130<y<235) or (60<x<260 and 110<y<125) or (140<x<180 and 90<y<112) or (48<x<78 and 152<y<177) or (242<x<272 and 152<y<177)
            if index==8:draw=((x-160)**2+(y-180)**2<75**2 or (x-101)**2+(y-107)**2<29**2 or (x-219)**2+(y-107)**2<29**2) and not ((x-132)**2+(y-169)**2<7**2 or (x-188)**2+(y-169)**2<7**2 or (x-160)**2+(y-205)**2<12**2)
            color=ink if draw else bg
            if draw and (y%35<3):color=tuple(min(c+20,255) for c in ink)
            data.extend(color)
    def chunk(kind,body):return struct.pack('!I',len(body))+kind+body+struct.pack('!I',zlib.crc32(kind+body)&0xffffffff)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(data))+chunk(b'IEND',b''))


def build(output):
    output=Path(output).resolve()
    if output.exists() or output.is_relative_to(ROOT):raise ValueError('Output must be new and outside repository')
    privacy=(ROOT/'miniprogram/src/services/privacy.ts').read_text('utf-8')
    if "const APPROVED_PRIVACY_APP_ID = '';" not in privacy:raise ValueError('Unexpected production privacy configuration')
    shutil.copytree(ROOT/'miniprogram',output,ignore=shutil.ignore_patterns('*.js','project.private.config.json','node_modules'))
    source=output/'src'
    config=json.loads((output/'project.config.json').read_text('utf-8'))
    config.update(appid='touristappid',projectname='开礼优选-离线UI演示',description='仅本机UI验收，虚拟数据，无微信授权或真实交易')
    (output/'project.config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copyfile(ROOT/'tools/ui-demo/transport.ts',source/'demo-transport.ts')
    for name in ('catalog','member-session','activation'):
        file=source/f'services/{name}.ts'
        content=file.read_text('utf-8').replace('wx.request(', 'demoRequest(').replace('wx.login(', 'demoLogin(')
        if name=='catalog':content=content.replace('return getApiOrigin() + path;', 'return path;')
        file.write_text("import {demoRequest,demoLogin} from '../demo-transport';\n"+content,encoding='utf-8')
    # Independent simulation module; never set the production approval constant.
    (source/'services/privacy.ts').write_text('''// OFFLINE UI SIMULATION ONLY: not WeChat privacy approval.
export class PrivacyAuthorizationError extends Error {}
export async function getPrivacySetting(){return {needAuthorization:false,contractName:'离线演示，不代表微信隐私授权'};}
export async function assertPrivacyAuthorized(){}
export async function openPrivacyContract(){throw new PrivacyAuthorizationError('这是离线UI演示，正式隐私指引尚未提交审核');}
''',encoding='utf-8')
    for file in (source/'services/cart.ts',source/'services/orders.ts'):
        file.write_text(file.read_text('utf-8').replace('mall_cart_v1','mall_ui_demo_cart_v1').replace('mall_checkout_pending_v1','mall_ui_demo_pending_v1'),encoding='utf-8')
    for file in (source/'pages').glob('*/index.wxml'):
        content=file.read_text('utf-8')
        file.write_text('<view class="demo-banner">离线UI演示 · 虚拟资料 · 请勿输入真实个人信息<navigator url="/pages/demo-console/index">切换演示场景 ›</navigator></view>'+content,encoding='utf-8')
    with (source/'app.wxss').open('a',encoding='utf-8') as stream:
        stream.write('\n.demo-banner{flex-shrink:0;padding:14rpx 28rpx;background:#fff1cd;color:#6a4b0b;font-size:22rpx;line-height:1.6}.demo-banner navigator{color:#153d39;font-weight:600}\n')
    app=json.loads((source/'app.json').read_text('utf-8'));app['pages'].append('pages/demo-console/index')
    app['window']['navigationBarTitleText']='开礼优选 · 离线演示'
    (source/'app.json').write_text(json.dumps(app,ensure_ascii=False,indent=2),encoding='utf-8')
    console=source/'pages/demo-console';console.mkdir()
    (console/'index.ts').write_text('''import {demoState,setScenario,resetDemo,advance,clearDemoAddress} from '../../demo-transport';
import {clearMemberSession} from '../../services/member-session';
Page({data:{scenario:'',orders:[],available:'',choices:[['normal','正常'],['empty','空商品'],['offline','网络失败'],['expired','会话失效'],['missing_supplier','受托资料未配置'],['price_conflict','条件变化'],['insufficient','积分不足'],['timeout_after_submit','提交成功但响应丢失']]},
onShow(){this.setData(demoState());},choose(e){setScenario(e.currentTarget.dataset.value);this.onShow();},
advance(e){advance(e.currentTarget.dataset.id);this.onShow();},clear(e){clearDemoAddress(e.currentTarget.dataset.id);this.onShow();},
reset(){resetDemo();clearMemberSession();wx.setStorageSync('mall_ui_demo_cart_v1',[]);wx.setStorageSync('mall_ui_demo_pending_v1:MEM-DEMO-001',null);this.onShow();},
catalog(){wx.reLaunch({url:'/pages/catalog/index'});},profile(){wx.reLaunch({url:'/pages/profile/index'});}});
''',encoding='utf-8')
    (console/'index.wxml').write_text('''<view class="screen"><text class="heading">离线演示控制台</text><text class="copy">本页只切换虚拟数据，不操作真实后台、积分或订单。当前：{{scenario}}；可用积分：{{available}}</text><button wx:for="{{choices}}" wx:key="index" data-value="{{item[0]}}" bindtap="choose">{{item[1]}}</button><button bindtap="catalog">返回商品页</button><button bindtap="profile">前往会员页</button><text class="copy">激活演示可输入：DEMO / 13800000000 / 演示车牌 / 23456789AB</text><view wx:for="{{orders}}" wx:key="order_public_id" class="mall-surface"><text class="copy">{{item.order_public_id}} · {{item.status}}</text><button data-id="{{item.order_public_id}}" bindtap="advance">模拟下一履约状态</button><button data-id="{{item.order_public_id}}" bindtap="clear">模拟终态收货信息已清理</button></view><button bindtap="reset">重置全部演示资料</button></view>''',encoding='utf-8')
    (console/'index.wxss').write_text('.screen{padding:28rpx}.heading{font-size:36rpx;font-weight:600}.copy{display:block;margin:20rpx 0}button{margin:16rpx 0}',encoding='utf-8')
    (console/'index.json').write_text('{"navigationBarTitleText":"离线演示控制台"}',encoding='utf-8')
    for i in range(9):png(source/f'uploads/mall_products/PRD-DEMO-00{i+1}/main.png',i)
    # Audit every generated TS file: there must be no native network or login call.
    for file in source.rglob('*.ts'):
        text=file.read_text('utf-8')
        if any(call in text for call in ('wx.request(', 'wx.login(', 'wx.uploadFile(', 'wx.downloadFile(')):
            raise ValueError('Unexpected live transport in demo')
    manifest={'mode':'OFFLINE_UI_ONLY','real_platform_acceptance':False,'production_privacy_unchanged':True,
              'source_privacy_sha256':hashlib.sha256(privacy.encode()).hexdigest(),
              'files':{str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file()}}
    (output/'demo-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'请先阅读.txt').write_text('仅本机微信开发者工具UI演示。使用虚拟数据，不输入真实资料；不代表真实微信授权、真实后端或真机验收。\n导入本目录即可，使用游客AppID，不连接本机服务或公网API。\n每页顶部可进入场景控制台。请先使用正常场景：浏览商品→加入购物车→会员页模拟登录→地址→确认兑换→订单。控制台可模拟履约、退款、收货信息清理和异常状态。\n图片为程序绘制的占位示意，非正式商品图；最终UI由用户在微信开发者工具验收。\n不要上传或发布本演示项目。正式代码的隐私审核通过标志保持为空。\n',encoding='utf-8')
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True)
    print(build(parser.parse_args().output))

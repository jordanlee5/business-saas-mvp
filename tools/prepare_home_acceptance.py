"""Create a NEW, loopback-only synthetic acceptance package outside the repository."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def build(output, port):
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError('Must use a new directory outside the business repository')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid local port')
    # Only committed, explicitly selected application sources. Never copy a working DB/config/upload.
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(['git', 'archive', 'HEAD', 'app', 'migrations', 'miniprogram',
                                      'alembic.ini', 'tools/ui-demo/transport.ts'], cwd=ROOT)
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output/'application'; snapshot.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(snapshot, filter='data')
    shutil.copytree(ROOT/'tools/home_acceptance', output/'harness')
    identity = secrets.token_hex(6)
    settings = dict(mode='LOCAL_SYNTHETIC_ACCEPTANCE_ONLY', source_commit=commit, port=port,
                    identity=identity, bootstrap=secrets.token_urlsafe(32),
                    signing_key=secrets.token_urlsafe(48), phone_key=secrets.token_hex(32),
                    operator_password=secrets.token_urlsafe(18))
    write(output/'private-local.json', json.dumps(settings, ensure_ascii=False, indent=2))
    (output/'data').mkdir()
    prepare_project(output, settings)
    samples(output)
    manifest = dict(mode=settings['mode'], source_commit=commit, port=port, synthetic_only=True,
        production_code={str(p.relative_to(snapshot)):hashlib.sha256(p.read_bytes()).hexdigest()
            for top in ('app','migrations') for p in (snapshot/top).rglob('*') if p.is_file()},
        harness_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (output/'harness').iterdir() if p.is_file()},
        original_miniprogram_modified=False, actual_sms_enabled=False)
    write(output/'acceptance-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    startup(output)
    write(output/'验收说明.txt', f'''本目录为独立虚拟验收包，不上传或发布。
入口：http://127.0.0.1:{port}/__acceptance
1. 打开入口，点“进入运营后台”；只修改本包的虚拟数据库及图片。
2. 可用“素材包.zip”里的图片，也可选择自己的无敏感信息测试图片。
3. 网页联动预览用于快速核对；原生布局/滑动请在微信开发者工具新建窗口导入本目录的 miniprogram。
4. 游客 AppID，仅本机模拟器，项目名包含“独立验收”；不要切换或覆盖原测试工程，不上传/不提审。
5. 初次进入小程序，点虚拟登录，再回首页；后台保存后返回首页或下拉刷新。
6. 手机号正式入口显示未启用；虚拟演示只用页面注明的示例号码，本地显示验证码，不发真实短信。
关闭电脑后入口暂停，重新开机双击“启动验收.cmd”。数据保留；不要删除 data 或覆盖旧包。
''')
    return manifest


def startup(output):
    # Restart only this package; refuse an occupied port or a changed local identity.
    script='''param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$root = '__ROOT__'
$settings = Get-Content -LiteralPath (Join-Path $root 'private-local.json') -Raw | ConvertFrom-Json
$url = 'http://127.0.0.1:' + $settings.port
$ready = $false
try {
    $state = Invoke-RestMethod -Uri ($url + '/__acceptance/health') -TimeoutSec 2
    if ($state.identity -ne $settings.identity) { throw '验收端口由其他环境使用，未启动新服务。' }
    $ready = $true
} catch {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try { $probe.Connect('127.0.0.1', [int]$settings.port); $occupied = $true } catch { $occupied = $false } finally { $probe.Dispose() }
    if ($occupied) { throw '验收端口已被占用，请联系助手核对；不会停止其他服务。' }
}
if (-not $ready) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
    $stdout = Join-Path $root ('data/server-' + $stamp + '.out.log')
    $stderr = Join-Path $root ('data/server-' + $stamp + '.err.log')
    $runner = Join-Path $root 'harness/runtime.py'
    $arguments = '-B "' + $runner + '" --root "' + $root + '"'
    $job = Start-Process -FilePath '__PYTHON__' -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath (Join-Path $root 'data/server.pid') -Value $job.Id
    for ($i=0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try { $state = Invoke-RestMethod -Uri ($url + '/__acceptance/health') -TimeoutSec 1; if ($state.identity -eq $settings.identity) { $ready = $true; break } } catch {}
        if ($job.HasExited) { break }
    }
    if (-not $ready) { throw '独立验收未启动成功，请联系助手核对本目录日志；原测试环境不受影响。' }
}
if (-not $NoBrowser) { Start-Process ($url + '/__acceptance') }
Write-Output ('独立验收入口已就绪：' + $url + '/__acceptance')
'''
    script=script.replace('__ROOT__',str(output).replace("'","''")).replace('__PYTHON__',sys.executable.replace("'","''"))
    (output/'start.ps1').write_text(script,encoding='utf-8-sig')
    write(output/'启动验收.cmd','@echo off\nchcp 65001 >nul\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "'+str(output/'start.ps1')+'"\nif errorlevel 1 pause\n')


def prepare_project(output, settings):
    snapshot=output/'application'; project=output/'miniprogram'
    shutil.copytree(snapshot/'miniprogram',project)
    src=project/'src'; origin=f"http://127.0.0.1:{settings['port']}"
    config=json.loads((project/'project.config.json').read_text(encoding='utf-8'))
    config.update(appid='touristappid',projectname='瑞泓开礼-三项独立验收-'+settings['identity'][:4],
                  description='虚拟验收，仅本机；不上传、不发布、不接短信')
    config['setting'].update(urlCheck=False)
    write(project/'project.config.json',json.dumps(config,ensure_ascii=False,indent=2))
    # Changes below are generated-copy adapters, never edits to the formal mini-program.
    for name in ('catalog','member-session','activation'):
        p=src/f'services/{name}.ts';s=p.read_text(encoding='utf-8')
        s=s.replace('wx.request(', 'demoRequest(').replace('wx.login(', 'demoLogin(')
        write(p,"import {demoRequest,demoLogin} from '../demo-transport';\n"+s)
    s=(snapshot/'tools/ui-demo/transport.ts').read_text(encoding='utf-8')
    s=s.replace('// @ts-nocheck',"// @ts-nocheck\nimport { homepageRequest } from './acceptance-bridge';",1)
    s=s.replace('export function demoRequest(options){',"export function demoRequest(options){if (/\\/home-(entries|banners)$/.test(options.url)) { homepageRequest(options); return; }")
    write(src/'demo-transport.ts',s)
    write(src/'acceptance-bridge.ts',(output/'harness/bridge.ts').read_text(encoding='utf-8'))
    write(src/'acceptance-local.ts','// LOCAL SYNTHETIC PACKAGE ONLY. Never upload this project.\n'+
          'export const localOrigin='+json.dumps(origin)+';\nexport const bootstrap='+json.dumps(settings['bootstrap'])+';\n')
    p=src/'config.ts';write(p,p.read_text(encoding='utf-8').replace('http://127.0.0.1:8000',origin))
    p=src/'services/catalog-images.ts';s=p.read_text(encoding='utf-8')
    s="import { acceptanceDownload } from '../acceptance-bridge';\n"+s
    s=s.replace('return downloadPrivateImage(imagePath(path, productId));','return Promise.resolve(imagePath(path, productId));')
    s=s.replace('wx.downloadFile(', 'acceptanceDownload(')
    write(p,s)
    write(src/'services/privacy.ts','''// LOCAL SYNTHETIC ACCEPTANCE ONLY; no actual WeChat privacy approval.
export class PrivacyAuthorizationError extends Error {}
export async function getPrivacySetting(){return {needAuthorization:false,contractName:'虚拟验收'};}
export async function assertPrivacyAuthorized(){}
export async function openPrivacyContract(){throw new PrivacyAuthorizationError('独立虚拟验收，不代表正式隐私审核');}
''')
    for p in src.rglob('*.ts'):
        s=p.read_text(encoding='utf-8')
        for name in ('mall_cart_v1','mall_checkout_pending_v1','mall-private-images'):
            s=s.replace(name,name+'-acceptance-'+settings['identity'])
        write(p,s)
    for p in (src/'pages').glob('*/index.wxml'):
        write(p,'<view class="acceptance-banner">独立验收 · 虚拟资料 · 仅连接本机</view>'+p.read_text(encoding='utf-8'))
    for p in src.rglob('*.wxml'):
        write(p,p.read_text(encoding='utf-8').replace('微信登录','虚拟登录'))
    p=src/'app.wxss';write(p,p.read_text(encoding='utf-8')+'\n.acceptance-banner{flex-shrink:0;padding:12rpx 24rpx;background:#fff1cd;color:#6a4b0b;font-size:22rpx;line-height:1.6}\n')
    # Virtual product art is packaged locally; homepage edits use real authenticated local downloads.
    from PIL import Image,ImageDraw
    for i in range(1,10):
        p=src/f'uploads/mall_products/PRD-DEMO-{i:03d}/main.png';p.parent.mkdir(parents=True,exist_ok=True)
        img=Image.new('RGB',(320,320),'#e5eddf');d=ImageDraw.Draw(img)
        d.rounded_rectangle((85,75,235,265),35,fill=['#b4c8aa','#dcc8a5','#8eafa2'][i%3]);img.save(p)
    for p in src.rglob('*.ts'):
        if p.name=='acceptance-bridge.ts':continue
        if any(call in p.read_text(encoding='utf-8') for call in ('wx.request(', 'wx.login(', 'wx.downloadFile(', 'wx.uploadFile(')):
            raise ValueError('Unexpected network call outside guarded local bridge')


def samples(output):
    from PIL import Image,ImageDraw,ImageFont
    folder=output/'素材包';folder.mkdir()
    font_path=Path('C:/Windows/Fonts/msyh.ttc')
    font=lambda n:ImageFont.truetype(str(font_path),n) if font_path.exists() else ImageFont.load_default(size=n)
    names=['居家日用','滋味零食','食品生鲜','首饰箱包','数码家电','全球美妆','厨房用品','母婴儿童']
    for i,name in enumerate(names,1):
        img=Image.new('RGB',(600,600),['#e6eddf','#faefdf','#e2ece8','#f4e8e1'][i%4]);d=ImageDraw.Draw(img)
        d.rounded_rectangle((155,120,445,400),60,fill=['#8aaa7d','#d2ac70','#779d8e','#b99280'][i%4])
        d.text((300,240),str(i),font=font(100),anchor='mm',fill='white')
        d.text((300,475),name,font=font(44),anchor='mm',fill='#153d39')
        d.text((300,548),'虚拟验收素材',font=font(23),anchor='mm',fill='#526e62')
        img.save(folder/f'{i:02d}-{name}.png')
    for i,(title,background) in enumerate([('春日焕新','#245d49'),('暖冬好物','#153d39'),('换季替换','#486d50')],1):
        img=Image.new('RGB',(1200,480),background);d=ImageDraw.Draw(img)
        d.ellipse((825,70,1115,360),fill='#c8d9b7');d.rounded_rectangle((880,150,1060,370),28,fill='#eae4d3')
        d.text((80,130),title,font=font(64),fill='#ffffff');d.text((84,245),'把每一份积分，换成喜欢的生活。',font=font(30),fill='#e0ebdb')
        d.text((84,375),f'虚拟验收轮播 {i} · 不是正式运营素材',font=font(24),fill='#d1e0cf')
        img.save(folder/f'轮播-{i}-{title}.png')
    with zipfile.ZipFile(output/'素材包.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for p in folder.iterdir():archive.write(p,'素材包/'+p.name)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True);parser.add_argument('--port',type=int,required=True)
    args=parser.parse_args();manifest=build(args.output,args.port)
    print(json.dumps({'source_commit':manifest['source_commit'],'port':manifest['port'],'synthetic_only':True}))

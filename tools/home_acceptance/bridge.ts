// Generated local acceptance project ONLY. The formal application never imports this module.
import { localOrigin, bootstrap } from './acceptance-local';
import { getApiOrigin } from './config';
let signed = '', expires = 0, loading: Promise<string> | undefined;
function assertLocal() {
  if (getApiOrigin() !== localOrigin || !/^http:\/\/127\.0\.0\.1:\d+$/.test(localOrigin)) throw new Error('仅可在本机开发者工具验收');
}
function session(): Promise<string> {
  assertLocal();
  if (signed && Date.now() < expires) return Promise.resolve(signed);
  if (loading) return loading;
  const header={Accept:'application/json','X-Acceptance-Bootstrap':bootstrap};
  loading = new Promise<string>((resolve,reject)=>wx.request({url:localOrigin+'/__acceptance/member-session',method:'POST',timeout:8000,
    header,success(r){
      const data=r.data as {token:string;expires_in:number};
      if(r.statusCode!==200 || typeof data.token!=='string') { reject(new Error('本机验收入口未就绪'));return; }
      signed=data.token;expires=Date.now()+600000;resolve(signed);
    },fail(){reject(new Error('请先启动本机验收入口'));}
  })).finally(()=>{loading=undefined;});
  return loading;
}
export function homepageRequest(options:any) {
  const valid=options.url===localOrigin+'/api/miniprogram/v1/home-entries'||options.url===localOrigin+'/api/miniprogram/v1/home-banners';
  if(!valid){options.fail({errMsg:'非验收素材地址'});return;}
  session().then(token=>wx.request({...options,header:{...options.header,Authorization:'Bearer '+token}})).catch(()=>options.fail({errMsg:'本机素材读取失败'}));
}
export function acceptanceDownload(options:any) {
  if(!options.url.startsWith(localOrigin+'/uploads/mall_home/')&&!options.url.startsWith(localOrigin+'/uploads/mall_banners/')){
    options.fail({errMsg:'非验收素材图片'});return;
  }
  session().then(token=>wx.downloadFile({...options,header:{...options.header,Authorization:'Bearer '+token}})).catch(()=>options.fail({errMsg:'本机图片读取失败'}));
}

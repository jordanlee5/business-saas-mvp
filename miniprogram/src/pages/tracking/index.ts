import {getTracking,trackingLabels,type Tracking} from '../../services/tracking';
import {hasMemberSession} from '../../services/member-session';
import {beijingTimeText} from '../../services/display-time';
Page({
 data:{loading:false,hasSession:false,error:'',tracking:null as Tracking|null,events:[] as {timeText:string;description:string}[],updatedText:'',labels:trackingLabels},
 generation:0,orderId:'',parcelId:0,invalid:true,
 onLoad(options:{order?:string;parcel?:string}={}){
   this.orderId=options.order||'';this.parcelId=Number(options.parcel);
   this.invalid=!/^ORD-[A-Z0-9]{16}$/.test(this.orderId)||!Number.isSafeInteger(this.parcelId)||this.parcelId<1||this.parcelId>2147483647;
 },
 onShow(){this.load();},
 onHide(){this.generation++;this.setData({loading:false,hasSession:false,tracking:null,events:[],updatedText:'',error:''});},
 onUnload(){this.onHide();},
 async load(){
   if(this.data.loading)return;
   const gen=++this.generation,hasSession=hasMemberSession();
   this.setData({hasSession,tracking:null,events:[],updatedText:'',error:''});
   if(this.invalid){this.setData({error:'物流链接无效，请从订单包裹重新进入。'});return;}
   if(!hasSession)return;
   this.setData({loading:true});
   try{
     const tracking=await getTracking(this.orderId,this.parcelId);
     if(gen===this.generation&&hasMemberSession())this.setData({tracking,events:tracking.events.map(e=>({description:e.description,timeText:beijingTimeText(e.time)})),updatedText:tracking.updated_at?beijingTimeText(tracking.updated_at):''});
   }catch(e){if(gen===this.generation)this.setData({error:e instanceof Error?e.message:'物流查询暂不可用，请稍后重试',hasSession:hasMemberSession()});}
   finally{if(gen===this.generation)this.setData({loading:false});}
 },
 async onPullDownRefresh(){try{await this.load();}finally{wx.stopPullDownRefresh();}},
 onRetry(){this.load();},
 onOrder(){if(/^ORD-[A-Z0-9]{16}$/.test(this.orderId))wx.redirectTo({url:'/pages/orders/index?id='+this.orderId});else wx.redirectTo({url:'/pages/orders/index'});},
 onProfile(){wx.reLaunch({url:'/pages/profile/index'});}
});

import {hasMemberSession} from '../../services/member-session';
import {listOrders,getOrder,cancelOrder,statusLabels,type OrderSummary,type OrderDetail} from '../../services/orders';
Page({
 data:{items:[] as OrderSummary[],detail:null as OrderDetail|null,loading:false,busy:false,hasSession:false,error:'',page:1,total:0,labels:statusLabels,confirmCancel:false,detailMode:false,submitted:false,cartWarning:false,noticeExpanded:false},
 generation:0,targetOrderId:'',invalidRoute:false,fromCheckout:false,cartWarning:false,
 onLoad(options:{id?:string;submitted?:string;cart_warning?:string}={}){
   this.invalidRoute=options.id!==undefined&&!/^ORD-[A-Z0-9]{16}$/.test(options.id);
   this.targetOrderId=!this.invalidRoute&&options.id?options.id:'';
   this.fromCheckout=Boolean(this.targetOrderId)&&options.submitted==='1';
   this.cartWarning=this.fromCheckout&&options.cart_warning==='1';
 },
 onShow(){
   this.setData({loading:false,busy:false,hasSession:hasMemberSession()});
   if(this.invalidRoute){this.setData({detailMode:true,error:'订单链接无效，请查看全部订单。'});return;}
   if(this.targetOrderId)this.show(this.targetOrderId);else this.load(1);
 },
 onHide(){this.generation++;this.setData({items:[],detail:null,confirmCancel:false,error:'',noticeExpanded:false,submitted:false,cartWarning:false,hasSession:false});},
 onUnload(){this.onHide();},
 async load(page:number){
   const gen=++this.generation;const hasSession=hasMemberSession();
   this.targetOrderId='';this.invalidRoute=false;this.fromCheckout=false;this.cartWarning=false;
   this.setData({items:[],detail:null,detailMode:false,hasSession,loading:hasSession,error:'',busy:false,confirmCancel:false,noticeExpanded:false,submitted:false,cartWarning:false});
   if(!hasSession)return;
   try{const result=await listOrders(page);if(gen===this.generation)this.setData({items:result.items,total:result.total,page});}
   catch(e){if(gen===this.generation)this.setData({error:e instanceof Error?e.message:'订单暂不可用',hasSession:hasMemberSession()});}
   finally{if(gen===this.generation)this.setData({loading:false});}
 },
 async show(id:string){
   if(this.data.busy||this.data.loading||!/^ORD-[A-Z0-9]{16}$/.test(id))return;
   this.targetOrderId=id;const gen=++this.generation;const hasSession=hasMemberSession();
   this.setData({loading:hasSession,hasSession,error:'',items:[],detail:null,detailMode:true,confirmCancel:false,noticeExpanded:false,submitted:false,cartWarning:false});
   if(!hasSession)return;
   try{const detail=await getOrder(id);if(gen===this.generation)this.setData({detail,submitted:this.fromCheckout,cartWarning:this.cartWarning});}
   catch(e){if(gen===this.generation)this.setData({error:e instanceof Error?e.message:'订单暂不可用',hasSession:hasMemberSession()});}
   finally{if(gen===this.generation)this.setData({loading:false});}
 },
 onDetail(e:{currentTarget:{dataset:{id:string}}}){this.show(e.currentTarget.dataset.id);},
 onToggleDeliveryInfo(){this.setData({noticeExpanded:!this.data.noticeExpanded});},
 onCancel(){if(this.data.detail?.status==='CREATED'&&!this.data.busy)this.setData({confirmCancel:true});},
 onKeep(){this.setData({confirmCancel:false});},
 async onConfirmCancel(){
   if(!this.data.confirmCancel||this.data.busy||!this.data.detail)return;
   const gen=this.generation;const id=this.data.detail.order_public_id;this.setData({busy:true,error:''});
   try{const detail=await cancelOrder(id);if(gen===this.generation){this.fromCheckout=false;this.setData({detail,confirmCancel:false,submitted:false});}}
   catch(e){if(gen===this.generation)this.setData({detail:null,confirmCancel:false,error:'取消结果请刷新本单核对',hasSession:hasMemberSession()});}
   finally{if(gen===this.generation)this.setData({busy:false});}
 },
 onReload(){if(!this.data.busy&&!this.data.loading){if(this.targetOrderId)this.show(this.targetOrderId);else if(!this.invalidRoute)this.load(this.data.page);}},
 onBack(){if(!this.data.busy&&!this.data.loading)this.load(this.data.page);},
 onPrevious(){if(!this.data.loading&&!this.data.busy&&this.data.page>1)this.load(this.data.page-1);},
 onNext(){if(!this.data.loading&&!this.data.busy&&this.data.page*20<this.data.total)this.load(this.data.page+1);},
 onCatalog(){wx.reLaunch({url:'/pages/catalog/index'});},
 onProfile(){wx.reLaunch({url:'/pages/profile/index'});}
});

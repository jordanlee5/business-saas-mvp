import {hasMemberSession} from '../../services/member-session';
import {listOrders,getOrder,cancelOrder,statusLabels,type OrderSummary,type OrderDetail} from '../../services/orders';
Page({
 data:{items:[] as OrderSummary[],detail:null as OrderDetail|null,loading:false,busy:false,hasSession:false,error:'',page:1,total:0,labels:statusLabels,confirmCancel:false},generation:0,
 onShow(){this.load(1);},onHide(){this.generation++;this.setData({items:[],detail:null,confirmCancel:false,error:''});},onUnload(){this.onHide();},
 async load(page:number){const gen=++this.generation;const hasSession=hasMemberSession();this.setData({items:[],detail:null,hasSession,loading:hasSession,error:'',busy:false,confirmCancel:false});if(!hasSession)return;
 try{const result=await listOrders(page);if(gen===this.generation)this.setData({items:result.items,total:result.total,page});}catch(e){if(gen===this.generation)this.setData({error:e instanceof Error?e.message:'订单暂不可用',hasSession:hasMemberSession()});}finally{if(gen===this.generation)this.setData({loading:false});}},
 async show(id:string){if(this.data.busy || this.data.loading)return;const gen=++this.generation;this.setData({loading:true,error:'',detail:null,confirmCancel:false});
 try{const detail=await getOrder(id);if(gen===this.generation)this.setData({detail});}catch(e){if(gen===this.generation)this.setData({error:e instanceof Error?e.message:'订单暂不可用',hasSession:hasMemberSession()});}finally{if(gen===this.generation)this.setData({loading:false});}},
 onDetail(e:{currentTarget:{dataset:{id:string}}}){this.show(e.currentTarget.dataset.id);},
 onCancel(){if(this.data.detail?.status==='CREATED'&&!this.data.busy)this.setData({confirmCancel:true});},
 onKeep(){this.setData({confirmCancel:false});},
 async onConfirmCancel(){if(!this.data.confirmCancel || this.data.busy || !this.data.detail)return;const gen=this.generation;const id=this.data.detail.order_public_id;this.setData({busy:true,error:''});
 try{const detail=await cancelOrder(id);if(gen===this.generation)this.setData({detail,confirmCancel:false});}catch(e){if(gen===this.generation)this.setData({detail:null,confirmCancel:false,error:'取消结果请重新打开订单核对',hasSession:hasMemberSession()});}finally{if(gen===this.generation)this.setData({busy:false});}},
 onReload(){if(!this.data.busy&&!this.data.loading){if(this.data.detail)this.show(this.data.detail.order_public_id);else this.load(this.data.page);}},
 onBack(){if(!this.data.busy)this.load(this.data.page);},onPrevious(){if(!this.data.loading&&!this.data.busy&&this.data.page>1)this.load(this.data.page-1);},onNext(){if(!this.data.loading&&!this.data.busy&&this.data.page*20<this.data.total)this.load(this.data.page+1);},
 onProfile(){wx.reLaunch({url:'/pages/profile/index'});}
});

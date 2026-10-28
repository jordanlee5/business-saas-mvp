import {getSignedInMember,hasMemberSession,MemberSessionError} from '../../services/member-session';
import {listAddresses,type Address} from '../../services/addresses';
import {readCart,loadCartDisplay,removeCartLine,type CartDisplayLine} from '../../services/cart';
import {beginCheckout,submitCheckout,resolveCheckout,pendingCheckout,clearPendingCheckout,canRetryCheckout,pendingCartLines,type OrderDetail} from '../../services/orders';
Page({
  data:{loading:false,busy:false,owner:'',addresses:[] as Address[],selected:'',items:[] as CartDisplayLine[],total:'',error:'',pending:false,canRetry:false,reviewing:false,complete:''},
  generation:0,
  pendingAction:null as Promise<void>|null,
  lastCompleted:null as {owner:string;id:string}|null,
  async onShow(){const gen=++this.generation;if(this.pendingAction){try{await this.pendingAction;}catch(_){}}if(gen===this.generation)this.load();},
  onHide(){this.generation++;this.setData({addresses:[],items:[],selected:'',owner:'',total:'',error:''});},
  onUnload(){this.onHide();},
  async load(review=false){
    const gen=++this.generation;
    this.setData({busy:false,loading:true,owner:'',addresses:[],items:[],selected:'',error:'',pending:false,reviewing:review,complete:''});
    try {
      const member=await getSignedInMember();if(!member)throw new Error('请先登录会员');
      if(gen!==this.generation)return;
      const owner=member.member_public_id;
      if(this.lastCompleted?.owner===owner){this.setData({owner,complete:this.lastCompleted.id});return;}
      if(pendingCheckout(owner) && !review){this.setData({owner,pending:true,canRetry:canRetryCheckout(owner)});return;}
      const addresses=await listAddresses();const cart=await loadCartDisplay(review ? pendingCartLines(owner) : readCart());
      if(gen!==this.generation)return;
      if(!cart.items.length || cart.hasUnavailable)throw new Error('请返回购物车核对可兑换商品');
      this.setData({owner,addresses,items:cart.items,total:cart.estimate,selected:addresses.find(x=>x.is_default)?.address_public_id || ''});
    }catch(e){if(gen===this.generation)this.setData({error:e instanceof Error?e.message:'暂不可用'});}
    finally{if(gen===this.generation)this.setData({loading:false});}
  },
  onSelect(e:{currentTarget:{dataset:{id:string}}}){if(!this.data.busy)this.setData({selected:e.currentTarget.dataset.id});},
  async act(query:boolean){
    if(this.data.busy || this.data.loading || !this.data.owner || !hasMemberSession())return;
    const gen=this.generation;const owner=this.data.owner;this.setData({busy:true,error:''});
    try{
      if(!pendingCheckout(owner) || this.data.reviewing){
        const address=this.data.addresses.find((x:Address)=>x.address_public_id===this.data.selected);
        if(!address)throw new Error('请选择收货地址');
        beginCheckout(owner,address.address_public_id,address,this.data.items);
        this.setData({reviewing:false});
      }
      const cartLines=pendingCartLines(owner);
      const result:OrderDetail=query?await resolveCheckout(owner):await submitCheckout(owner);
      // Clear the recovery marker only after a validated successful response.
      clearPendingCheckout(owner);
      this.lastCompleted={owner,id:result.order_public_id};
      let warning='';
      try{for(const item of cartLines){const current=readCart().find(x=>x.product_public_id===item.product_public_id && x.sku_code===item.sku_code);if(current && current.quantity===item.quantity)removeCartLine(item.product_public_id,item.sku_code);}}
      catch(_){warning='订单已成功，请手动核对购物车';}
      if(gen!==this.generation)return;
      this.setData({complete:result.order_public_id,pending:false,addresses:[],items:[],selected:'',error:warning});
    }catch(e){
      if(gen!==this.generation)return;
      // Preserve the same request key until a validated order is found, even across app restarts.
      this.setData({pending:Boolean(pendingCheckout(owner)),canRetry:canRetryCheckout(owner),error:e instanceof Error?e.message:'结果暂不确定，请查询订单'});
    }finally{if(gen===this.generation)this.setData({busy:false});}
  },
  onSubmit(){if(!this.data.busy)this.pendingAction=this.act(false);},onQuery(){if(!this.data.busy)this.pendingAction=this.act(true);},
  onReviewPending(){if(!this.data.busy)this.load(true);},
  onReload(){if(!this.data.busy)this.load();},
  onAddresses(){wx.navigateTo({url:'/pages/addresses/index'});},
  onOrders(){wx.navigateTo({url:'/pages/orders/index'});},
  onProfile(){wx.reLaunch({url:'/pages/profile/index'});},
});

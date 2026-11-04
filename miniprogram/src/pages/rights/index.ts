import { hasMemberSession, requestMemberPrivate } from '../../services/member-session';

type Summary = {public_id:string; kind:string; status:string; created_at:string};
type HistoryEntry = {action:string; note:string; time:string};
type Detail = Summary & {description:string; history:HistoryEntry[]};
type DetailView = Detail & {history:(HistoryEntry & {sequence:number})[]};
const kinds = ['ACCESS','CORRECT','DELETE','WITHDRAW','CLOSE'];
Page({
  data: {labels:['查阅','更正','删除','撤回','注销'], kind:0, description:'', signedIn:false,
    busy:false, error:'', notice:'', page:1, total:0, items:[] as Summary[], detail:null as DetailView|null},
  generation:0,
  key:'',
  pendingWrite:null as Promise<unknown>|null,
  async onShow() {
    const generation = this.generation;
    const signedIn = hasMemberSession();
    this.setData({signedIn});
    if (!signedIn) return;
    if (this.pendingWrite) {
      this.setData({busy:true});
      await this.pendingWrite.catch(() => undefined);
    }
    if (generation === this.generation) await this.load(1);
  },
  onHide() { this.clearView(); },
  onUnload() { this.clearView(); },
  clearView() {
    this.generation++;
    this.key='';
    this.setData({signedIn:false, busy:false, kind:0, description:'', items:[], detail:null, total:0, error:'',notice:''});
  },
  report(error:unknown) {
    if (!hasMemberSession()) this.clearView();
    this.setData({error:error instanceof Error ? error.message : '暂时无法取得申请记录'});
  },
  async load(page:number) {
    const generation=this.generation;
    this.setData({busy:true,error:''});
    try {
      const data=await requestMemberPrivate('/rights-requests?page='+page,'GET') as {items:Summary[];total:number};
      if (generation===this.generation) this.setData({items:data.items,total:data.total,page,detail:null});
    } catch(error) { if (generation===this.generation) this.report(error); }
    finally { if (generation===this.generation) this.setData({busy:false}); }
  },
  pick(e:{detail:{value:string}}) { if (!this.data.busy) {this.key='';this.setData({kind:Number(e.detail.value)});} },
  input(e:{detail:{value:string}}) { if (!this.data.busy) {this.key='';this.setData({description:e.detail.value});} },
  async submit() {
    if (this.data.busy || !hasMemberSession()) return;
    const description=this.data.description.trim();
    if (!description) {this.setData({error:'请填写申请说明'});return;}
    const generation=this.generation;
    if (!this.key) this.key=Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
    this.setData({busy:true,error:'',notice:''});
    try {
      this.pendingWrite = requestMemberPrivate('/rights-requests','POST',{
        request_key:this.key, kind:kinds[this.data.kind], description
      });
      await this.pendingWrite;
      if (generation!==this.generation) return;
      this.key=''; this.setData({description:'',notice:'申请已提交，可在下方查看进度'});
      await this.load(1);
    } catch(error) { if (generation===this.generation) this.report(error); }
    finally {
      this.pendingWrite = null;
      if (generation===this.generation) this.setData({busy:false});
    }
  },
  async open(e:{currentTarget:{dataset:{id:string}}}) {
    if (this.data.busy) return;
    const generation=this.generation;
    this.setData({busy:true,error:'',detail:null});
    try {
      const detail=await requestMemberPrivate('/rights-requests/'+e.currentTarget.dataset.id,'GET') as Detail;
      if (generation===this.generation) {
        const history = detail.history.map((event,index) => ({...event,sequence:index+1}));
        this.setData({detail:{...detail,history}});
      }
    } catch(error) { if (generation===this.generation) this.report(error); }
    finally { if (generation===this.generation) this.setData({busy:false}); }
  },
  refresh() { if(!this.data.busy) this.load(this.data.page); },
  previous() { if(!this.data.busy && this.data.page>1) this.load(this.data.page-1); },
  next() { if(!this.data.busy && this.data.page*20<this.data.total) this.load(this.data.page+1); },
  back() { this.setData({detail:null}); },
});

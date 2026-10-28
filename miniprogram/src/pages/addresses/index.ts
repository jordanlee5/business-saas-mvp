import { blankAddress, fields, normalizeAddress, listAddresses, saveAddress, removeAddress, defaultAddress, type Address } from '../../services/addresses';
import { hasMemberSession, MemberSessionError } from '../../services/member-session';

Page({
  data: { items: [] as Address[], form: blankAddress(), editing: false, editId: '', deleteId: '',
    deleteDefault: false, loading: false, busy: false, ready: false, hasSession: false, error: '', notice: '' },
  generation: 0,
  pendingWrite: null as Promise<void> | null,
  async onShow() {
    const current = ++this.generation;
    if (this.pendingWrite) {
      this.setData({ loading: true, hasSession: hasMemberSession() });
      try { await this.pendingWrite; } catch (_) { /* Refresh determines the result. */ }
    }
    if (current === this.generation) this.refresh();
  },
  onHide() { this.clearView(); },
  onUnload() { this.clearView(); },
  clearView() {
    this.generation += 1;
    this.setData({ items: [], form: blankAddress(), editing: false, editId: '', deleteId: '',
      deleteDefault: false, busy: false, loading: false, ready: false, error: '', notice: '' });
  },
  async refresh() {
    const current = ++this.generation;
    const hasSession = hasMemberSession();
    this.setData({ items: [], form: blankAddress(), editing: false, editId: '', deleteId: '',
      ready: false, hasSession, loading: hasSession, busy: false, error: '' });
    if (!hasSession) return;
    try {
      const items = await listAddresses();
      if (current === this.generation) this.setData({ items, ready: true });
    } catch (error) {
      if (current === this.generation) this.setData({ hasSession: hasMemberSession(), error: error instanceof Error ? error.message : '地址服务暂不可用' });
    } finally { if (current === this.generation) this.setData({ loading: false }); }
  },
  canAct() {
    if (!hasMemberSession()) { this.clearView(); this.setData({ hasSession: false }); return false; }
    return this.data.ready && !this.data.busy && !this.data.loading;
  },
  onAddTap() {
    if (!this.canAct() || this.data.items.length >= 20) return;
    this.setData({ form: blankAddress(), editId: '', editing: true, deleteId: '', error: '', notice: '' });
  },
  onEditTap(event: { currentTarget: { dataset: { id: string } } }) {
    if (!this.canAct()) return;
    const item = this.data.items.find((row: Address) => row.address_public_id === event.currentTarget.dataset.id);
    if (item) this.setData({ form: normalizeAddress(item), editId: item.address_public_id, editing: true, deleteId: '', error: '', notice: '' });
  },
  onInput(event: { currentTarget: { dataset: { field: string } }; detail: { value: string } }) {
    if (!this.canAct() || !this.data.editing) return;
    const field = event.currentTarget.dataset.field;
    if (fields.includes(field as typeof fields[number])) this.setData({ ['form.' + field]: event.detail.value });
  },
  onCancelTap() { if (!this.data.busy) this.setData({ editing: false, form: blankAddress(), editId: '', deleteId: '', error: '' }); },
  async mutate(action: () => Promise<void>) {
    if (!this.canAct()) return;
    const current = this.generation;
    this.setData({ busy: true, error: '', notice: '' });
    try {
      const pending = action();
      this.pendingWrite = pending;
      try { await pending; } finally { if (this.pendingWrite === pending) this.pendingWrite = null; }
      if (current !== this.generation) return;
      this.setData({ notice: '操作成功' });
      await this.refresh();
    } catch (error) {
      if (current !== this.generation) return;
      const message = error instanceof Error ? error.message : '操作结果暂不确定，请刷新后核对';
      // Never automatically repeat a write after a timeout or ambiguous response.
      if (error instanceof MemberSessionError && error.status === 422) this.setData({ error: message, busy: false });
      else {
        this.setData({ items: [], form: blankAddress(), editing: false, editId: '', deleteId: '',
          ready: false, busy: false, hasSession: hasMemberSession(), error: message });
      }
    }
  },
  onSaveTap() {
    if (!this.canAct() || !this.data.editing) return;
    let form;
    try { form = normalizeAddress(this.data.form); }
    catch (error) { this.setData({ error: error instanceof Error ? error.message : '请检查地址' }); return; }
    this.mutate(() => saveAddress(this.data.editId, form));
  },
  onDeleteTap(event: { currentTarget: { dataset: { id: string } } }) {
    if (!this.canAct()) return;
    const item = this.data.items.find((row: Address) => row.address_public_id === event.currentTarget.dataset.id);
    if (item) this.setData({ deleteId: item.address_public_id, deleteDefault: item.is_default, editing: false, form: blankAddress(), editId: '', error: '' });
  },
  onConfirmDeleteTap() { if (this.data.deleteId) this.mutate(() => removeAddress(this.data.deleteId)); },
  onDefaultTap(event: { currentTarget: { dataset: { id: string } } }) {
    if (!this.canAct()) return;
    const item = this.data.items.find((row: Address) => row.address_public_id === event.currentTarget.dataset.id);
    if (item && !item.is_default) this.mutate(() => defaultAddress(item.address_public_id));
  },
  onRetryTap() { if (!this.data.busy && !this.data.loading) this.refresh(); },
  onProfileTap() { wx.reLaunch({ url: '/pages/profile/index' }); },
});

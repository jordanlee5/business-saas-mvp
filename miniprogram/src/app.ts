import { clearMemberSession } from './services/member-session';
import { invalidateCatalog } from './services/catalog-state';
import { clearCatalogImages } from './services/catalog-images';
App({
  onLaunch() { clearMemberSession(); clearCatalogImages(); },
  onHide() { invalidateCatalog('checking', false); },
});

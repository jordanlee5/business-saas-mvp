import { getApiOrigin } from '../config';
import { memberDownloadContext, MemberSessionError } from './member-session';
import { catalogEpoch, onCatalogInvalidated } from './catalog-state';

const cache = new Map<string, string>();
const pending = new Map<string, Promise<string>>();
let sequence = 0, active = 0;
const queue: Array<() => void> = [];
function directory(): string { return wx.env.USER_DATA_PATH + '/mall-private-images'; }
function remove(path: string): void {
  try { wx.getFileSystemManager().unlinkSync(path); } catch (_) { /* Retry at next cleanup/startup. */ }
}
export function clearCatalogImages(): void {
  cache.clear(); pending.clear();
  try {
    const fs = wx.getFileSystemManager(), dir = directory();
    for (const file of fs.readdirSync(dir)) {
      if (/^image-[0-9]+-[0-9]+\.[a-z]+$/.test(file)) remove(dir + '/' + file);
    }
  } catch (_) { /* Directory may not exist yet; never sweep unrelated files. */ }
}
onCatalogInvalidated(clearCatalogImages);

export function forgetCatalogImage(path: string): void {
  const local = cache.get(path);
  cache.delete(path);
  if (local) remove(local);
}

function imagePath(path: string | null, productId?: string): string {
  if (!path || !/^\/uploads\/mall_products\/[A-Z0-9-]{1,32}\/[A-Za-z0-9_-]+(?:\.thumb-640)?\.(?:png|jpg|jpeg|webp)$/i.test(path)) return '';
  if (productId && !path.startsWith('/uploads/mall_products/' + productId + '/')) return '';
  return path;
}

export function downloadCatalogImage(path: string | null, productId?: string): Promise<string> {
  return downloadPrivateImage(imagePath(path, productId));
}

export function downloadHomeEntryImage(path: string | null, key: string): Promise<string> {
  const safe = typeof path === 'string' &&
    /^\/uploads\/mall_home\/(home|food|fresh|bags|digital|beauty|kitchen|kids)\/[0-9a-f]{32}\.webp$/.test(path) &&
    path.startsWith('/uploads/mall_home/' + key + '/') ? path : '';
  return downloadPrivateImage(safe);
}

function downloadPrivateImage(safe: string): Promise<string> {
  if (!safe) return Promise.resolve('');
  let context: ReturnType<typeof memberDownloadContext>;
  try { context = memberDownloadContext(); } catch (error) { return Promise.reject(error); }
  const epoch = catalogEpoch();
  if (cache.has(safe)) {
    const local = cache.get(safe)!;
    cache.delete(safe); cache.set(safe, local);
    return Promise.resolve(local);
  }
  if (pending.has(safe)) return pending.get(safe)!;
  const task = new Promise<string>((resolve, reject) => {
    const run = () => {
      active += 1;
      let target = '', finished = false;
      const done = (value: string, error?: unknown) => {
        if (finished) return;
        finished = true; active -= 1;
        if (epoch === catalogEpoch()) pending.delete(safe);
        if (error) reject(error); else resolve(value);
        const next = queue.shift(); if (next) next();
      };
      try {
        context.assertCurrent();
        const fs = wx.getFileSystemManager(), dir = directory();
        try { fs.accessSync(dir); } catch (_) { fs.mkdirSync(dir, true); }
        // Evict least-recent files so later pages can still download after a long browse.
        while (cache.size && fs.readdirSync(dir).length + active > 80) {
          const oldest = cache.keys().next().value as string;
          forgetCatalogImage(oldest);
        }
        // Bound disk growth even if deletion failed; display a placeholder, never a public URL.
        if (fs.readdirSync(dir).length + active > 80) { done(''); return; }
        target = dir + '/image-' + Date.now() + '-' + (++sequence) + '.' + safe.split('.').pop();
        wx.downloadFile({url: getApiOrigin() + safe, header: context.header, timeout: 8000, filePath: target,
          success(result) {
            try {
              context.assertCurrent(); context.rejectStatus(result.statusCode);
              if (result.statusCode !== 200) { remove(target); done(''); return; }
              cache.set(safe, target);
              done(target);
            } catch (error) { remove(target); done('', error); }
          },
          fail() {
            remove(target);
            try { context.assertCurrent(); done(''); } catch (error) { done('', error); }
          },
        });
      } catch (error) {
        if (target) remove(target);
        done('', error instanceof MemberSessionError ? error : undefined);
      }
    };
    if (active < 4) run(); else queue.push(run);
  });
  pending.set(safe, task);
  // Also handles synchronous mocked failures without retaining a settled promise.
  task.then(() => { if (pending.get(safe) === task) pending.delete(safe); }, () => {
    if (pending.get(safe) === task) pending.delete(safe);
  });
  return task;
}

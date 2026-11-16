"""Replacement entry, retired-form safety, and unchanged historical records."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from app import main, models
from app.admin_permissions import OPERATOR, PRIMARY_REVIEWER
import test_company_banners as banner_fixtures


class CompanyNavigationTests(unittest.TestCase):
    setUp = banner_fixtures.CompanyBannerTests.setUp

    def test_single_existing_menu_opens_only_current_materials(self):
        page=self.client.get('/company-materials')
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.text.count('href="/company-materials"'),1)
        self.assertIn('宣传页配置',page.text)
        self.assertIn('企业宣传页素材',page.text)
        self.assertIn('side-link active',page.text)
        self.assertNotIn('href="/promotion-pages"',page.text)
        self.assertNotIn('name="slug"',page.text)
        for url in ('/promotion-pages','/promotion-pages?edit_id=123','/promotion-pages/123/preview'):
            response=self.client.get(url)
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['location'],'/company-materials')

    def test_stale_forms_cannot_write_or_remove_historical_records(self):
        with self.database.begin() as db:
            row=models.PromotionPage(slug='legacy-virtual',company_name='虚拟企业',page_title='历史资料',
                is_published=True,created_by_id=self.user_id,updated_by_id=self.user_id)
            db.add(row);db.flush();row_id=row.id
        urls=('/promotion-pages',f'/promotion-pages/{row_id}/edit',f'/promotion-pages/{row_id}/images',
              f'/promotion-pages/{row_id}/images/1/delete',f'/promotion-pages/{row_id}/publication')
        for url in urls:
            response=self.client.post(url,data={'page_title':'不应生效','is_published':'false'})
            self.assertEqual(response.status_code,410)
            self.assertIn('no-store',response.headers['cache-control'])
        with self.database() as db:
            row=db.get(models.PromotionPage,row_id)
            self.assertEqual(row.page_title,'历史资料');self.assertTrue(row.is_published)
            self.assertEqual(db.query(models.PromotionPage).count(),1)
            self.assertEqual(db.query(models.CompanyBanner).count(),0)
        self.assertEqual(self.client.get('/promo/legacy-virtual').status_code,200)
        self.assertFalse(self.root.exists())

    def test_legacy_entry_keeps_operator_only_permissions(self):
        for user in (None, SimpleNamespace(id=self.user_id,role='admin',admin_level=PRIMARY_REVIEWER,
                                            is_active=True,must_change_password=False)):
            with patch.object(main,'get_current_user',return_value=user):
                expected=302 if user is None else 403
                response=self.client.get('/promotion-pages')
                self.assertEqual(response.status_code,expected)
                self.assertEqual(self.client.post('/promotion-pages').status_code,401 if user is None else 403)
                if user is None:
                    self.assertEqual(response.headers['location'],'/login')


if __name__=='__main__':unittest.main()

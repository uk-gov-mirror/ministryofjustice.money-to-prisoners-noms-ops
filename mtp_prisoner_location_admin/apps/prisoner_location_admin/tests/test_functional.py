import os

from mtp_common.test_utils.functional_tests import FunctionalTestCase


class PrisonerLocationAdminTestCase(FunctionalTestCase):
    """
    Base class for all prisoner-location-admin functional tests
    """
    accessibility_scope_selector = '#content'

    def login_and_go_to(self, link_text):
        self.login('prisoner-location-admin', 'prisoner-location-admin')
        self.driver.find_element_by_partial_link_text(link_text).click()


class ErrorTests(PrisonerLocationAdminTestCase):
    """
    Tests for general errors
    """
    def test_404(self):
        self.driver.get(self.live_server_url + '/unknown-page/')
        self.assertInSource('Page not found')


class LoginTests(PrisonerLocationAdminTestCase):
    """
    Tests for Login page
    """

    def test_title(self):
        self.driver.get(self.live_server_url)
        heading = self.driver.find_element_by_tag_name('h1')
        self.assertEqual('Upload prisoner locations', heading.text)
        self.assertEqual('48px', heading.value_of_css_property('font-size'))

    def test_bad_login(self):
        self.login('prisoner-location-admin', 'bad-password')
        self.assertInSource('There was a problem')

    def test_good_login(self):
        self.login('prisoner-location-admin', 'prisoner-location-admin')
        self.assertCurrentUrl('/')
        self.assertInSource('Upload location file')

    def test_logout(self):
        self.login('prisoner-location-admin', 'prisoner-location-admin')
        self.driver.find_element_by_link_text('Sign out').click()
        self.assertCurrentUrl('/login/')


class UploadTests(PrisonerLocationAdminTestCase):
    """
    Tests for Upload functionality
    """

    def setUp(self):
        super().setUp()
        self.login('prisoner-location-admin', 'prisoner-location-admin')
        self.driver.execute_script('document.getElementById("id_location_file").style.left = 0')

    def test_checking_upload_page(self):
        self.assertInSource('Upload location file')
        self.assertInSource('Use this page to upload a prisoner location file in CSV format (.csv)')
        self.assertCssProperty('.upload-otherfilelink', 'display', 'none')

    def test_upload_valid_file(self):
        el = self.driver.find_element_by_xpath('//input[@type="file"]')
        el.send_keys(os.path.join(os.path.dirname(__file__), 'files', 'valid.csv'))
        self.assertInSource('Change file')
        el.submit()
        self.assertInSource('316 prisoner locations updated successfully')
        self.assertCssProperty('.upload-otherfilelink', 'display', 'block')

    def test_upload_invalid_file(self):
        el = self.driver.find_element_by_xpath('//input[@type="file"]')
        el.send_keys(os.path.join(os.path.dirname(__file__), 'files', 'invalid.csv'))
        el.submit()
        self.assertInSource('Row has 4 columns, should have 5')

    def test_upload_empty_file(self):
        el = self.driver.find_element_by_xpath('//input[@type="file"]')
        el.send_keys(os.path.join(os.path.dirname(__file__), 'files', 'empty.csv'))
        el.submit()
        self.assertInSource('Location file does not seem to contain any valid rows')

    def test_submit_file_upload_without_selecting_file(self):
        el = self.driver.find_element_by_xpath('//input[@type="file"]')
        el.submit()
        self.assertInSource('This field is required')

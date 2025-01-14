import datetime
from math import ceil

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv4_address
from django.template.defaultfilters import pluralize
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _
from mtp_common.forms.fields import SplitDateField

from security.forms.object_base import (
    AmountPattern,
    parse_amount,
    SecurityForm,
    validate_amount,
    validate_prisoner_number,
    validate_range_fields,
)
from security.models import credit_sources, disbursement_methods, PaymentMethod
from security.utils import (
    convert_date_fields,
    remove_whitespaces_and_hyphens,
    sender_profile_name,
)

PRISON_SELECTOR_USER_PRISONS_CHOICE_VALUE = 'mine'

END_DATE_BEFORE_START_DATE_ERROR_MSG = _('Must be after the start date.')


class SearchFormV2Mixin(forms.Form):
    """
    Mixin for SearchForm V2.
    """
    # indicates whether the form was used in advanced search
    advanced = forms.BooleanField(initial=False, required=False)

    def get_ga_event_category(self):
        """GA event category."""
        return f'form-errors-{self.__class__.__name__}'

    def was_advanced_search_used(self):
        return self.cleaned_data.get('advanced', False)

    def get_api_request_params(self):
        """
        Removes `advanced` from the API call as it's not a valid filter.
        """
        api_params = super().get_api_request_params()
        api_params.pop('advanced', None)
        return api_params


class PrisonSelectorSearchFormMixin(forms.Form):
    """
    Mixin with prison fields for search V2.

    prison_selector can be one of:
    - all: all prisons
    - mine: current user's prisons
    - exact: for a specific prison

    when prison_selector == exact a `prison` value must be specified
    otherwise the form isn't valid.
    when prison_selector != exact, any `prison` value is reset as not applicable
    """
    PRISON_SELECTOR_EXACT_PRISON_CHOICE_VALUE = 'exact'
    PRISON_SELECTOR_ALL_PRISONS_CHOICE_VALUE = 'all'

    prison_selector = forms.ChoiceField(
        label=_('Prison'),
        required=False,
        choices=(
            (PRISON_SELECTOR_USER_PRISONS_CHOICE_VALUE, _('Your prisons')),
            (PRISON_SELECTOR_ALL_PRISONS_CHOICE_VALUE, _('All prisons')),
            (PRISON_SELECTOR_EXACT_PRISON_CHOICE_VALUE, _('A specific prison')),
        ),
        initial=PRISON_SELECTOR_USER_PRISONS_CHOICE_VALUE,
    )
    prison = forms.MultipleChoiceField(label=_('Prison name'), required=False, choices=[])

    def _update_prison_in_query_data(self, query_data):
        prison_selector = query_data.pop('prison_selector', None)
        prisons = query_data.pop('prison', [])

        if prison_selector == PRISON_SELECTOR_USER_PRISONS_CHOICE_VALUE:
            if self.request.user_prisons:
                query_data['prison'] = [
                    prison['nomis_id']
                    for prison in self.request.user_prisons
                ]
        elif prison_selector == self.PRISON_SELECTOR_EXACT_PRISON_CHOICE_VALUE or not prison_selector:
            query_data['prison'] = prisons

    def get_query_data(self, allow_parameter_manipulation=True):
        """
        Updates `query_data` by translating prison_selector into the appropriate prison value for the API.
        """
        query_data = super().get_query_data(allow_parameter_manipulation=allow_parameter_manipulation)
        if allow_parameter_manipulation:
            self._update_prison_in_query_data(query_data)
        return query_data

    def _clean_prison_fields(self, cleaned_data):
        # if prison related fields are already in error don't check any further
        if set(self.errors) & {'prison_selector', 'prison'}:
            return cleaned_data

        prison_selector = cleaned_data.get('prison_selector', None)

        if prison_selector == self.PRISON_SELECTOR_EXACT_PRISON_CHOICE_VALUE:
            if not cleaned_data.get('prison'):
                self.add_error(
                    'prison',
                    ValidationError(
                        self.fields['prison'].error_messages['required'],
                        code='required',
                    ),
                )
        else:
            cleaned_data['prison'] = []

        return cleaned_data

    def clean(self):
        """
        Validates the prison related fields and resets the prison field
        if incompatible with the choosen prison_selector.
        """
        cleaned_data = super().clean()
        return self._clean_prison_fields(cleaned_data)

    def allow_all_prisons_simple_search(self):
        """
        :return: True if the current simple search could benefit from extending the search
            to all prisons.

        This is the case when:
        - a simple search was made using a non-empty search term
        - the default prisons value of the user who made the search is not 'all'
        """
        if not hasattr(self, 'cleaned_data') or not self.cleaned_data.get('simple_search'):
            return False

        return (
            self.cleaned_data['prison_selector'] == PRISON_SELECTOR_USER_PRISONS_CHOICE_VALUE
            and self.request.user_prisons
        )

    def was_all_prisons_simple_search_used(self):
        """
        :return: True if a simple search in all prisons was made, False otherwise.
        """
        if not hasattr(self, 'cleaned_data') or not self.cleaned_data.get('simple_search'):
            return False

        return (
            self.cleaned_data['prison_selector'] == self.PRISON_SELECTOR_ALL_PRISONS_CHOICE_VALUE
            and self.request.user_prisons
        )

    def get_extra_search_description_template_kwargs(self):
        user_prisons_tot = len(self.request.user_prisons or [])

        if self.was_all_prisons_simple_search_used() or not user_prisons_tot:
            prisons_filter_description = 'in all prisons'
        else:
            prisons_filter_description = f'in your selected prison{pluralize(user_prisons_tot)}'

        return {
            'prisons_filter_description': prisons_filter_description,
        }


class AmountSearchFormMixin(forms.Form):
    """
    Mixin for the amount fields and related logic.
    """
    amount_pattern = forms.ChoiceField(
        label=_('Amount'),
        required=False,
        choices=AmountPattern.get_choices(),
        initial='',
    )
    amount_exact = forms.CharField(
        label=AmountPattern.exact.value,
        validators=[validate_amount],
        required=False,
    )
    amount_pence = forms.IntegerField(
        label=AmountPattern.pence.value,
        min_value=0,
        max_value=99,
        required=False,
    )

    def _update_amounts_in_query_data(self, query_data):
        amount_pattern = query_data.pop('amount_pattern', None)
        try:
            amount_pattern = AmountPattern[amount_pattern]
        except KeyError:
            return

        amount_exact = query_data.pop('amount_exact', None)
        amount_pence = query_data.pop('amount_pence', None)

        if amount_pattern == AmountPattern.not_integral:
            query_data['exclude_amount__endswith'] = '00'
        elif amount_pattern == AmountPattern.not_multiple_5:
            query_data['exclude_amount__regex'] = '(500|000)$'
        elif amount_pattern == AmountPattern.not_multiple_10:
            query_data['exclude_amount__endswith'] = '000'
        elif amount_pattern == AmountPattern.gte_100:
            query_data['amount__gte'] = '10000'
        elif amount_pattern == AmountPattern.exact:
            query_data['amount'] = parse_amount(amount_exact or '', as_int=False)
        elif amount_pattern == AmountPattern.pence:
            query_data['amount__endswith'] = '' if amount_pence is None else '%02d' % amount_pence
        else:
            raise NotImplementedError

    def get_query_data(self, allow_parameter_manipulation=True):
        """
        Updates `query_data` by translating amount_pattern, amount_exact and amount_pence
        into appropriate filters for the API.
        """
        query_data = super().get_query_data(allow_parameter_manipulation=allow_parameter_manipulation)
        if allow_parameter_manipulation:
            self._update_amounts_in_query_data(query_data)
        return query_data

    def _clean_amount_fields(self, cleaned_data):
        # if amount fields are already in error don't check any further
        if set(self.errors) & {'amount_pattern', 'amount_exact', 'amount_pence'}:
            return cleaned_data

        try:
            amount_pattern = AmountPattern[cleaned_data.get('amount_pattern')]
        except KeyError:
            amount_pattern = None

        if amount_pattern == AmountPattern.exact:
            if not cleaned_data.get('amount_exact'):
                self.add_error(
                    'amount_exact',
                    ValidationError(_('This field is required for the selected amount pattern.'), code='required'),
                )
        else:
            cleaned_data['amount_exact'] = ''

        if amount_pattern == AmountPattern.pence:
            if cleaned_data.get('amount_pence') is None:
                self.add_error(
                    'amount_pence',
                    ValidationError(_('This field is required for the selected amount pattern.'), code='required'),
                )
        else:
            cleaned_data['amount_pence'] = ''
        return cleaned_data

    def clean(self):
        """
        Validates the amount fields and resets amount_exact, amount_pence if incompatible with
        the choosen amount_pattern.
        """
        cleaned_data = super().clean()
        return self._clean_amount_fields(cleaned_data)


class PaymentMethodSearchFormMixin(forms.Form):
    """
    Mixin for the payment method fields and related logic.
    """
    payment_method = forms.ChoiceField(
        label=_('Payment method'),
        required=False,
        choices=[],
        initial='',
    )
    account_number = forms.CharField(label=_('Account number'), required=False)
    sort_code = forms.CharField(label=_('Sort code'), required=False)
    card_number_last_digits = forms.CharField(
        label=_('Last 4 digits of card number'),
        max_length=4,
        required=False,
    )

    # mapping from this form fields to related API filters to be translated into
    # they can be overridden when subclassing if API endpoints expect different filter names
    PAYMENT_METHOD_API_FIELDS_MAPPING = {
        'payment_method': 'method',
        'account_number': 'account_number',
        'sort_code': 'sort_code',
        'card_number_last_digits': 'card_number_last_digits',
    }

    # scope for each field (e.g. account_number only applies if payment method == bank transfer)
    PAYMENT_METHOD_FIELDS_SCOPE = {
        'payment_method': [
            PaymentMethod.bank_transfer,
            PaymentMethod.online,
            PaymentMethod.cheque,
        ],
        'account_number': [PaymentMethod.bank_transfer],
        'sort_code': [PaymentMethod.bank_transfer],
        'card_number_last_digits': [PaymentMethod.online],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # set up choices for payment method based on actual subclass
        self.fields['payment_method'].choices = [
            ('', _('Any method')),  # blank option

            *self.get_payment_method_choices(),
        ]

        # store scope on fields so that it's easier to get them later on
        for field_name, supported_payment_methods in self.PAYMENT_METHOD_FIELDS_SCOPE.items():
            self.fields[field_name].supported_payment_methods = supported_payment_methods

    def get_payment_method_choices(self):
        """
        To be implemented when subclassing.

        Returns a list of tuples (id, name) to be used as payment method choices.
        """
        raise NotImplementedError()

    def _update_payment_method_in_query_data(self, query_data):
        try:
            payment_method = PaymentMethod[
                query_data.get('payment_method', None)
            ]
        except KeyError:
            payment_method = None

        for field_name in self.PAYMENT_METHOD_API_FIELDS_MAPPING:
            field = self.fields[field_name]
            cleaned_field_value = query_data.pop(field_name, None)

            if not cleaned_field_value:
                continue

            if payment_method in field.supported_payment_methods:
                api_filter_name = self.PAYMENT_METHOD_API_FIELDS_MAPPING[field_name]
                query_data[api_filter_name] = cleaned_field_value

    def get_query_data(self, allow_parameter_manipulation=True):
        """
        Updates `query_data` by translating payment method fields into appropriate filters for the API.
        """
        query_data = super().get_query_data(allow_parameter_manipulation=allow_parameter_manipulation)
        if allow_parameter_manipulation:
            self._update_payment_method_in_query_data(query_data)
        return query_data

    def clean_sort_code(self):
        sort_code = self.cleaned_data.get('sort_code')
        return remove_whitespaces_and_hyphens(sort_code)

    def _clean_payment_method_fields(self, cleaned_data):
        # if payment method fields are already in error don't check any further
        if set(self.errors) & set(self.PAYMENT_METHOD_FIELDS_SCOPE):
            return cleaned_data

        try:
            payment_method = PaymentMethod[
                cleaned_data.get('payment_method', None)
            ]
        except KeyError:
            payment_method = None

        for field_name in self.PAYMENT_METHOD_FIELDS_SCOPE:
            field = self.fields[field_name]
            if payment_method not in field.supported_payment_methods:
                cleaned_data[field_name] = ''
        return cleaned_data

    def clean(self):
        """
        Validates the payment method fields and resets the ones that are not compatible with
        the choosen method.
        """
        cleaned_data = super().clean()
        return self._clean_payment_method_fields(cleaned_data)


class SendersFormV2(
    SearchFormV2Mixin,
    PrisonSelectorSearchFormMixin,
    PaymentMethodSearchFormMixin,
    SecurityForm,
):
    """
    Search Form for Senders V2.
    """
    ordering = forms.ChoiceField(
        label=_('Order by'),
        required=False,
        initial='-prisoner_count',
        choices=[
            ('prisoner_count', _('Number of prisoners (low to high)')),
            ('-prisoner_count', _('Number of prisoners (high to low)')),
            ('prison_count', _('Number of prisons (low to high)')),
            ('-prison_count', _('Number of prisons (high to low)')),
            ('credit_count', _('Number of credits (low to high)')),
            ('-credit_count', _('Number of credits (high to low)')),
            ('credit_total', _('Total sent (low to high)')),
            ('-credit_total', _('Total sent (high to low)')),
        ]
    )
    simple_search = forms.CharField(
        label=_('Search payment source name or email address'),
        required=False,
        help_text=_('Common or incomplete names may show many results'),
    )
    sender_name = forms.CharField(label=_('Name'), required=False)
    sender_email = forms.CharField(label=_('Email'), required=False)
    sender_postcode = forms.CharField(label=_('Postcode'), required=False)

    # NB: ensure that these templates are HTML-safe
    filtered_description_template = 'Results containing {filter_description} {prisons_filter_description}'
    unfiltered_description_template = ''

    description_templates = (
        ('“{simple_search}”',),
    )
    description_capitalisation = {}
    unlisted_description = ''

    PAYMENT_METHOD_API_FIELDS_MAPPING = {
        'payment_method': 'source',
        'account_number': 'sender_account_number',
        'sort_code': 'sender_sort_code',
        'card_number_last_digits': 'card_number_last_digits',
    }

    def get_object_list_endpoint_path(self):
        return '/senders/'

    def get_payment_method_choices(self):
        return credit_sources.items()

    def clean_sender_postcode(self):
        sender_postcode = self.cleaned_data.get('sender_postcode')
        return remove_whitespaces_and_hyphens(sender_postcode)

    def _clean_sender_fields(self, cleaned_data):
        """
        Validates that some sender fields are not used when payment method != `debit card`.
        """
        if 'payment_method' not in cleaned_data:
            return cleaned_data

        try:
            payment_method = PaymentMethod[
                cleaned_data['payment_method']
            ]
        except KeyError:
            payment_method = None

        if payment_method == PaymentMethod.online:
            return cleaned_data

        for field_name in ('sender_email', 'sender_postcode'):
            if cleaned_data.get(field_name):
                self.add_error(
                    field_name,
                    ValidationError(
                        _('Only available for debit card payments.'),
                        code='conflict',
                    ),
                )

        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        return self._clean_sender_fields(cleaned_data)


class PrisonersFormV2(SearchFormV2Mixin, PrisonSelectorSearchFormMixin, SecurityForm):
    """
    Search Form for Prisoners V2.
    """
    ordering = forms.ChoiceField(
        label=_('Order by'),
        required=False,
        initial='-sender_count',
        choices=[
            ('sender_count', _('Number of senders (low to high)')),
            ('-sender_count', _('Number of senders (high to low)')),
            ('credit_count', _('Number of credits (low to high)')),
            ('-credit_count', _('Number of credits (high to low)')),
            ('credit_total', _('Total received (low to high)')),
            ('-credit_total', _('Total received (high to low)')),
            ('recipient_count', _('Number of recipients (low to high)')),
            ('-recipient_count', _('Number of recipients (high to low)')),
            ('disbursement_count', _('Number of disbursements (low to high)')),
            ('-disbursement_count', _('Number of disbursements (high to low)')),
            ('disbursement_total', _('Total sent (low to high)')),
            ('-disbursement_total', _('Total sent (high to low)')),
            ('prisoner_name', _('Prisoner name (A to Z)')),
            ('-prisoner_name', _('Prisoner name (Z to A)')),
            ('prisoner_number', _('Prisoner number (A to Z)')),
            ('-prisoner_number', _('Prisoner number (Z to A)')),
        ],
    )
    simple_search = forms.CharField(
        label=_('Search prisoner number or name'),
        required=False,
        help_text=_('For example, name or “A1234BC”'),
    )
    prisoner_number = forms.CharField(
        label=_('Prisoner number'),
        validators=[validate_prisoner_number],
        required=False,
    )
    prisoner_name = forms.CharField(label=_('Prisoner name'), required=False)

    # NB: ensure that these templates are HTML-safe
    filtered_description_template = 'Results containing {filter_description} {prisons_filter_description}'
    unfiltered_description_template = ''

    description_templates = (
        ('“{simple_search}”',),
    )
    description_capitalisation = {}
    unlisted_description = ''

    def get_object_list_endpoint_path(self):
        return '/prisoners/'

    def clean_prisoner_number(self):
        """
        Make sure prisoner number is always uppercase.
        """
        prisoner_number = self.cleaned_data.get('prisoner_number')
        if not prisoner_number:
            return prisoner_number

        return prisoner_number.upper()

    def get_query_data(self, allow_parameter_manipulation=True):
        """
        Make sure the API call filters by `current_prison` instead of the `prison` field which queries
        all historic prisons.
        """
        query_data = super().get_query_data(allow_parameter_manipulation=allow_parameter_manipulation)
        if allow_parameter_manipulation:
            prisons = query_data.pop('prison', None)
            if prisons:
                query_data['current_prison'] = prisons
        return query_data


class CreditsFormV2(
    SearchFormV2Mixin,
    AmountSearchFormMixin,
    PrisonSelectorSearchFormMixin,
    PaymentMethodSearchFormMixin,
    SecurityForm,
):
    """
    Search Form for Credits V2.
    """
    ordering = forms.ChoiceField(
        label=_('Order by'),
        required=False,
        initial='-received_at',
        choices=[
            ('received_at', _('Received date (oldest to newest)')),
            ('-received_at', _('Received date (newest to oldest)')),
            ('amount', _('Amount sent (low to high)')),
            ('-amount', _('Amount sent (high to low)')),
            ('prisoner_name', _('Prisoner name (A to Z)')),
            ('-prisoner_name', _('Prisoner name (Z to A)')),
            ('prisoner_number', _('Prisoner number (A to Z)')),
            ('-prisoner_number', _('Prisoner number (Z to A)')),
        ],
    )
    simple_search = forms.CharField(
        label=_('Search payment source name, email address or prisoner number'),
        required=False,
        help_text=_('Common or incomplete names may show many results'),
    )
    received_at__gte = SplitDateField(
        label=_('From'),
        required=False,
        help_text=_('For example, 01 08 2007'),
    )
    received_at__lt = SplitDateField(
        label=_('To'),
        required=False,
        help_text=_('For example, 01 08 2007'),
    )

    sender_name = forms.CharField(label=_('Name'), required=False)
    sender_email = forms.CharField(label=_('Email'), required=False)
    sender_postcode = forms.CharField(label=_('Postcode'), required=False)
    sender_ip_address = forms.CharField(
        label=_('IP address'),
        validators=[validate_ipv4_address],
        required=False,
    )

    prisoner_name = forms.CharField(label=_('Prisoner name'), required=False)
    prisoner_number = forms.CharField(
        label=_('Prisoner number'),
        validators=[validate_prisoner_number],
        required=False,
    )

    exclusive_date_params = ['received_at__lt']

    # NB: ensure that these templates are HTML-safe
    filtered_description_template = 'Results containing {filter_description} {prisons_filter_description}'
    unfiltered_description_template = ''

    description_templates = (
        ('“{simple_search}”',),
    )
    description_capitalisation = {}
    unlisted_description = ''

    PAYMENT_METHOD_API_FIELDS_MAPPING = {
        'payment_method': 'source',
        'account_number': 'sender_account_number',
        'sort_code': 'sender_sort_code',
        'card_number_last_digits': 'card_number_last_digits',
    }

    def get_object_list(self):
        object_list = super().get_object_list()
        convert_date_fields(object_list)
        return object_list

    def get_object_list_endpoint_path(self):
        return '/credits/'

    def get_payment_method_choices(self):
        return credit_sources.items()

    def clean_sender_postcode(self):
        sender_postcode = self.cleaned_data.get('sender_postcode')
        return remove_whitespaces_and_hyphens(sender_postcode)

    def clean_prisoner_number(self):
        """
        Make sure prisoner number is always uppercase.
        """
        prisoner_number = self.cleaned_data.get('prisoner_number')
        if not prisoner_number:
            return prisoner_number

        return prisoner_number.upper()

    def get_query_data(self, allow_parameter_manipulation=True):
        """
        Split Date Fields are compressed into a datetime.date values.
        This is okay for API calls but when we need to preserve the query string
        (e.g. redirect to search results page or export), we need to keep the split
        values instead.
        """
        query_data = super().get_query_data(
            allow_parameter_manipulation=allow_parameter_manipulation,
        )

        if not allow_parameter_manipulation:
            for date_field_name in ('received_at__gte', 'received_at__lt'):
                value = query_data.pop(date_field_name, None)
                if not value:
                    continue

                query_data.update(
                    {
                        f'{date_field_name}_{index}': value_part
                        for index, value_part in enumerate(
                            SplitDateField().widget.decompress(value),
                        )
                    },
                )
        return query_data

    def _clean_dates(self, cleaned_data):
        """
        Validates dates.
        """
        received_at__gte = cleaned_data.get('received_at__gte')
        received_at__lt = cleaned_data.get('received_at__lt')

        if received_at__gte and received_at__lt and received_at__gte > received_at__lt:
            self.add_error(
                'received_at__lt',
                ValidationError(END_DATE_BEFORE_START_DATE_ERROR_MSG, code='bound_ordering'),
            )
        return cleaned_data

    def _clean_sender_fields(self, cleaned_data):
        """
        Validates that some sender fields are not used when payment method != `debit card`.
        """
        if 'payment_method' not in cleaned_data:
            return cleaned_data

        try:
            payment_method = PaymentMethod[
                cleaned_data['payment_method']
            ]
        except KeyError:
            payment_method = None

        if payment_method == PaymentMethod.online:
            return cleaned_data

        for field_name in ('sender_email', 'sender_postcode', 'sender_ip_address'):
            if cleaned_data.get(field_name):
                self.add_error(
                    field_name,
                    ValidationError(
                        _('Only available for debit card payments.'),
                        code='conflict',
                    ),
                )

        return cleaned_data

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data = self._clean_dates(cleaned_data)
        return self._clean_sender_fields(cleaned_data)


class DisbursementsFormV2(
    SearchFormV2Mixin,
    AmountSearchFormMixin,
    PrisonSelectorSearchFormMixin,
    PaymentMethodSearchFormMixin,
    SecurityForm,
):
    """
    Search Form for Disbursements V2.
    """
    ordering = forms.ChoiceField(
        label=_('Order by'),
        required=False,
        initial='-created',
        choices=[
            ('created', _('Date entered (oldest to newest)')),
            ('-created', _('Date entered (newest to oldest)')),
            ('amount', _('Amount sent (low to high)')),
            ('-amount', _('Amount sent (high to low)')),
            ('prisoner_name', _('Prisoner name (A to Z)')),
            ('-prisoner_name', _('Prisoner name (Z to A)')),
            ('prisoner_number', _('Prisoner number (A to Z)')),
            ('-prisoner_number', _('Prisoner number (Z to A)')),
        ],
    )
    simple_search = forms.CharField(
        label=_('Search recipient name or prisoner number'),
        required=False,
        help_text=_('Common or incomplete names may show many results'),
    )
    created__gte = SplitDateField(
        label=_('From'),
        required=False,
        help_text=_('For example, 01 08 2007'),
    )
    created__lt = SplitDateField(
        label=_('To'),
        required=False,
        help_text=_('For example, 01 08 2007'),
    )

    recipient_name = forms.CharField(label=_('Name'), required=False)
    recipient_email = forms.CharField(label=_('Email'), required=False)
    postcode = forms.CharField(label=_('Postcode'), required=False)

    prisoner_name = forms.CharField(label=_('Prisoner name'), required=False)
    prisoner_number = forms.CharField(
        label=_('Prisoner number'),
        validators=[validate_prisoner_number],
        required=False,
    )

    invoice_number = forms.CharField(label=_('Invoice number'), required=False)

    exclusive_date_params = ['created__lt']

    # NB: ensure that these templates are HTML-safe
    filtered_description_template = 'Results containing {filter_description} {prisons_filter_description}'
    unfiltered_description_template = ''

    description_templates = (
        ('“{simple_search}”',),
    )
    description_capitalisation = {}
    unlisted_description = ''
    exclude_private_estate = True

    def get_object_list(self):
        object_list = super().get_object_list()
        convert_date_fields(object_list)
        return object_list

    def get_object_list_endpoint_path(self):
        return '/disbursements/'

    def get_payment_method_choices(self):
        return disbursement_methods.items()

    def clean_postcode(self):
        postcode = self.cleaned_data.get('postcode')
        return remove_whitespaces_and_hyphens(postcode)

    def clean_prisoner_number(self):
        """
        Make sure prisoner number is always uppercase.
        """
        prisoner_number = self.cleaned_data.get('prisoner_number')
        if not prisoner_number:
            return prisoner_number

        return prisoner_number.upper()

    def get_query_data(self, allow_parameter_manipulation=True):
        """
        Split Date Fields are compressed into a datetime.date values.
        This is okay for API calls but when we need to preserve the query string
        (e.g. redirect to search results page or export), we need to keep the split
        values instead.
        """
        query_data = super().get_query_data(
            allow_parameter_manipulation=allow_parameter_manipulation,
        )

        if not allow_parameter_manipulation:
            for date_field_name in ('created__gte', 'created__lt'):
                value = query_data.pop(date_field_name, None)
                if not value:
                    continue

                query_data.update(
                    {
                        f'{date_field_name}_{index}': value_part
                        for index, value_part in enumerate(
                            SplitDateField().widget.decompress(value),
                        )
                    },
                )
        return query_data

    def _clean_dates(self, cleaned_data):
        created__gte = cleaned_data.get('created__gte')
        created__lt = cleaned_data.get('created__lt')

        if created__gte and created__lt and created__gte > created__lt:
            self.add_error(
                'created__lt',
                ValidationError(END_DATE_BEFORE_START_DATE_ERROR_MSG, code='bound_ordering'),
            )
        return cleaned_data

    def clean(self):
        """
        Validates dates.
        """
        cleaned_data = super().clean()
        return self._clean_dates(cleaned_data)


@validate_range_fields(
    ('triggered_at', _('Must be after the start date'), '__lt'),
)
class NotificationsForm(SecurityForm):
    # NB: ensure that these templates are HTML-safe
    filtered_description_template = 'All notifications are shown below.'
    unfiltered_description_template = 'All notifications are shown below.'
    description_templates = ()

    page_size = 25

    def __init__(self, request, **kwargs):
        super().__init__(request, **kwargs)
        self.date_count = 0

    def get_object_list_endpoint_path(self):
        return '/events/'

    def get_query_data(self, allow_parameter_manipulation=True):
        query_data = super().get_query_data(allow_parameter_manipulation=allow_parameter_manipulation)
        if allow_parameter_manipulation:
            query_data['rule'] = ('MONP', 'MONS')
        return query_data

    def get_api_request_page_params(self):
        filters = super().get_api_request_page_params()
        if filters is not None:
            data = self.session.get('/events/pages/', params=filters).json()
            self.date_count = data['count']
            filters['ordering'] = '-triggered_at'
            del filters['offset']
            del filters['limit']
            if data['newest']:
                filters['triggered_at__lt'] = parse_date(data['newest']) + datetime.timedelta(days=1)
                filters['triggered_at__gte'] = parse_date(data['oldest'])
        return filters

    def get_object_list(self):
        events = convert_date_fields(super().get_object_list())
        date_groups = map(summarise_date_group, group_events_by_date(events))

        self.page_count = int(ceil(self.date_count / self.page_size))
        self.total_count = self.date_count
        return date_groups


def make_date_group(date):
    return {
        'date': date,
        'senders': {},
        'prisoners': {},
    }


def make_date_group_profile(profile_id, description):
    return {
        'id': profile_id,
        'description': description,
        'credit_ids': set(),
        'disbursement_ids': set(),
    }


def group_events_by_date(events):
    date_groups = []
    date_group = make_date_group(None)
    for event in events:
        event_date = event['triggered_at'].date()
        if event_date != date_group['date']:
            date_group = make_date_group(event_date)
            date_groups.append(date_group)

        if event['sender_profile']:
            profile = event['sender_profile']
            if profile['id'] in date_group['senders']:
                details = date_group['senders'][profile['id']]
            else:
                details = make_date_group_profile(
                    profile['id'],
                    sender_profile_name(profile)
                )
                date_group['senders'][profile['id']] = details
            if event['credit_id']:
                details['credit_ids'].add(event['credit_id'])
            if event['disbursement_id']:
                details['disbursement_ids'].add(event['disbursement_id'])

        if event['prisoner_profile']:
            profile = event['prisoner_profile']
            if profile['id'] in date_group['prisoners']:
                details = date_group['prisoners'][profile['id']]
            else:
                details = make_date_group_profile(
                    profile['id'],
                    f"{profile['prisoner_name']} ({profile['prisoner_number']})"
                )
                date_group['prisoners'][profile['id']] = details
            if event['credit_id']:
                details['credit_ids'].add(event['credit_id'])
            if event['disbursement_id']:
                details['disbursement_ids'].add(event['disbursement_id'])
    return date_groups


def summarise_date_group(date_group):
    date_group_transaction_count = 0

    sender_summaries = []
    senders = sorted(
        date_group['senders'].values(),
        key=lambda s: s['description']
    )
    for sender in senders:
        profile_transaction_count = len(sender['credit_ids'])
        date_group_transaction_count += profile_transaction_count
        sender_summaries.append({
            'id': sender['id'],
            'transaction_count': profile_transaction_count,
            'description': sender['description'],
        })

    prisoner_summaries = []
    prisoners = sorted(
        date_group['prisoners'].values(),
        key=lambda p: p['description']
    )
    for prisoner in prisoners:
        disbursements_only = bool(prisoner['disbursement_ids'] and not prisoner['credit_ids'])
        profile_transaction_count = len(prisoner['credit_ids']) + len(prisoner['disbursement_ids'])
        date_group_transaction_count += profile_transaction_count
        prisoner_summaries.append({
            'id': prisoner['id'],
            'transaction_count': profile_transaction_count,
            'description': prisoner['description'],
            'disbursements_only': disbursements_only,
        })

    return {
        'date': date_group['date'],
        'transaction_count': date_group_transaction_count,
        'senders': sender_summaries,
        'prisoners': prisoner_summaries,
    }

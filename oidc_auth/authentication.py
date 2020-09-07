import time

import requests
from django.contrib.auth import get_user_model
from django.utils.encoding import smart_str
from django.utils.functional import cached_property
from django.utils.translation import ugettext as _
from requests import request
from requests.exceptions import HTTPError
from rest_framework.authentication import (BaseAuthentication,
                                           get_authorization_header)
from rest_framework.exceptions import AuthenticationFailed

from authlib.jose import JsonWebKey, jwt
from authlib.oidc.core.claims import IDToken
from authlib.jose.errors import (BadSignatureError, DecodeError,
                                 ExpiredTokenError, JoseError)
from authlib.oidc.discovery import get_well_known_url

from .settings import api_settings
from .util import cache


def get_user_by_id(request, id_token):
    User = get_user_model()
    try:
        user = User.objects.get_by_natural_key(id_token.get('sub'))
    except User.DoesNotExist:
        msg = _('Invalid Authorization header. User not found.')
        raise AuthenticationFailed(msg)
    return user


class DRFIDToken(IDToken):

    def validate_exp(self, now, leeway):
        super().validate_exp(now, leeway)
        if now > self['exp']:
            msg = _('Invalid Authorization header. JWT has expired.')
            raise AuthenticationFailed(msg)

    def validate_iat(self, now, leeway):
        super().validate_iat(now, leeway)
        if self['iat'] < leeway:
            msg = _('Invalid Authorization header. JWT too old.')
            raise AuthenticationFailed(msg)

    def validate_aud(self):
        super().validate_aud()
        if isinstance(self['aud'], str):
            self['aud'] = [self['aud']]
        if len(self['aud']) > 1 and 'azp' not in self:
            msg = _('Invalid Authorization header. Missing JWT authorized party.')
            raise AuthenticationFailed(msg)
        else:
            super().validate_azp()



class BaseOidcAuthentication(BaseAuthentication):
    @property
    @cache(ttl=api_settings.OIDC_BEARER_TOKEN_EXPIRATION_TIME)
    def oidc_config(self):
        return requests.get(
            get_well_known_url(
                api_settings.OIDC_ENDPOINT,
                external=True
            )
        ).json()


class BearerTokenAuthentication(BaseOidcAuthentication):
    www_authenticate_realm = 'api'

    def authenticate(self, request):
        bearer_token = self.get_bearer_token(request)
        if bearer_token is None:
            return None

        try:
            userinfo = self.get_userinfo(bearer_token)
        except HTTPError:
            msg = _('Invalid Authorization header. Unable to verify bearer token')
            raise AuthenticationFailed(msg)

        user = api_settings.OIDC_RESOLVE_USER_FUNCTION(request, userinfo)

        return user, userinfo

    def get_bearer_token(self, request):
        auth = get_authorization_header(request).split()
        auth_header_prefix = api_settings.BEARER_AUTH_HEADER_PREFIX.lower()
        if not auth or smart_str(auth[0].lower()) != auth_header_prefix:
            return None

        if len(auth) == 1:
            msg = _('Invalid Authorization header. No credentials provided')
            raise AuthenticationFailed(msg)
        elif len(auth) > 2:
            msg = _(
                'Invalid Authorization header. Credentials string should not contain spaces.')
            raise AuthenticationFailed(msg)

        return auth[1]

    @cache(ttl=api_settings.OIDC_BEARER_TOKEN_EXPIRATION_TIME)
    def get_userinfo(self, token):
        response = requests.get(
            self.oidc_config['userinfo_endpoint'],
            headers={'Authorization': 'Bearer {0}'.format(
                token.decode('ascii')
            )
            }
        )
        response.raise_for_status()

        return response.json()


class JSONWebTokenAuthentication(BaseOidcAuthentication):
    """Token based authentication using the JSON Web Token standard"""

    www_authenticate_realm = 'api'

    def authenticate(self, request):
        jwt_value = self.get_jwt_value(request)
        if jwt_value is None:
            return None
        payload = self.decode_jwt(jwt_value)
        self.validate_claims(payload)

        user = api_settings.OIDC_RESOLVE_USER_FUNCTION(request, payload)

        return user, payload

    def get_jwt_value(self, request):
        auth = get_authorization_header(request).split()
        auth_header_prefix = api_settings.JWT_AUTH_HEADER_PREFIX.lower()

        if not auth or smart_str(auth[0].lower()) != auth_header_prefix:
            return None

        if len(auth) == 1:
            msg = _('Invalid Authorization header. No credentials provided')
            raise AuthenticationFailed(msg)
        elif len(auth) > 2:
            msg = _(
                'Invalid Authorization header. Credentials string should not contain spaces.')
            raise AuthenticationFailed(msg)

        return auth[1]

    # @cache(ttl=api_settings.OIDC_JWKS_EXPIRATION_TIME)
    def jwks(self):
        r = request("GET", self.oidc_config['jwks_uri'], allow_redirects=True)
        r.raise_for_status()
        keys = JsonWebKey.import_key_set(r.json())
        return keys

    @cached_property
    def issuer(self):
        return self.oidc_config['issuer']

    def decode_jwt(self, jwt_value: bytes):
        try:
            # assert isinstance(jwt_value, bytes)
            claims_options = {
                'iss': {
                    'essential': True,
                    'value': self.issuer
                },
                'aud': {
                    'values': self.get_audiences()
                }
            }

            id_token = jwt.decode(
                jwt_value.decode('ascii'),
                self.jwks(),
                claims_cls=DRFIDToken,
                claims_options=claims_options
            )

        except (BadSignatureError, DecodeError):
            msg = _(
                'Invalid Authorization header. JWT Signature verification failed.')
            raise AuthenticationFailed(msg)
        except AssertionError:
            msg = _(
                'Invalid Authorization header. Please provide base64 encoded ID Token'
            )
            raise AuthenticationFailed(msg)

        return id_token

    def get_audiences(self):
        return [audience for audience in api_settings.OIDC_AUDIENCES]

    def validate_claims(self, id_token):
        try:
            id_token.validate(
                now=int(time.time()),
                leeway=int(time.time()-api_settings.OIDC_LEEWAY)
            )
        except ExpiredTokenError:
            msg = _('Invalid Authorization header. JWT has expired.')
            raise AuthenticationFailed(msg)
        except JoseError as e:
            msg = _(str(type(e)) + str(e))
            raise AuthenticationFailed(msg)

    def authenticate_header(self, request):
        return 'JWT realm="{0}"'.format(self.www_authenticate_realm)

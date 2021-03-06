from django.conf import settings
from django.core.mail import mail_admins
from django.forms.util import ValidationError
from django.utils import simplejson as json
from django_facebook import settings as facebook_settings
from django_facebook.official_sdk import GraphAPI, GraphAPIError
from datetime import datetime
import hashlib
import hmac
import logging
import sys
import time


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def get_facebook_graph(request=None, access_token=None, persistent_token=facebook_settings.FACEBOOK_PERSISTENT_TOKEN):
    '''
    given a request from one of these
    - js authentication flow
    - facebook app authentication flow
    - mobile authentication flow
    
    store authentication data in the session
    
    returns a graph object
    '''
    if not request and persistent_token:
        raise ValidationError, 'Request is required if you want to use persistent tokens'
    from django_facebook import official_sdk
    
    additional_data = None
    facebook_open_graph_cached = False

    if persistent_token:
        logger.debug('Get cached facebook_open_graph ')
        facebook_open_graph_cached = request.session.get('facebook_open_graph')
    if facebook_open_graph_cached:
        #TODO: should handle this in class' pickle protocol, but this is easier
        facebook_open_graph_cached._is_authenticated = None
        
    signed_request = request.REQUEST.get('signed_request')
    cookie_name = 'fbs_%s' % facebook_settings.FACEBOOK_APP_ID
    oauth_cookie = request.COOKIES.get(cookie_name)
                
    #scenario A, we're on a canvas page and need to parse the signed data
    if signed_request:
        logger.debug('found signed request..')
        additional_data = FacebookAPI.parse_signed_data(signed_request)
        logger.debug('signed_data: '+str(additional_data))
        access_token = additional_data.get('oauth_token')
    #scenario B, we're using javascript and cookies to authenticate
    elif oauth_cookie:
        logger.debug('found oauth cookie...')
        additional_data = official_sdk.get_user_from_cookie(request.COOKIES, facebook_settings.FACEBOOK_APP_ID, facebook_settings.FACEBOOK_APP_SECRET)
        additional_data["user_id"] = additional_data["uid"]
        logger.debug('oauth cookie: signed_data: '+str(additional_data))
        access_token = additional_data.get('access_token')
    
    facebook_open_graph = FacebookAPI(access_token, additional_data)


    if facebook_open_graph.access_token and persistent_token:
        logger.debug('storing facebook_open_graph in session..')
        request.session['facebook_open_graph'] = facebook_open_graph
    elif facebook_open_graph_cached:
        logger.debug('Using cached facebook_open_graph ')
        facebook_open_graph = facebook_open_graph_cached


    # Check if the OAuth token has expired
    if facebook_open_graph.additional_data.has_key('expires'):
        if datetime.fromtimestamp(float(facebook_open_graph.additional_data['expires'])) < datetime.now():
            logger.debug('FB Access token expired, revalidate!')
            facebook_open_graph._is_authenticated = None

    return facebook_open_graph




class FacebookAPI(GraphAPI):
    '''
    Wrapper around the default facebook api with
    - support for creating django users
    - caches registration and profile data, ensuring
    efficient use of facebook connections
    '''
    def __init__(self, access_token=None, additional_data=None):
        self.access_token = access_token
        self.additional_data = additional_data
        
        self._is_authenticated = None
        self._is_authorized = None
        self._page = None
        self._user =  None
        self._user_id =  None
        self._profile = None
        GraphAPI.__init__(self, access_token)
        
    def __repr__(self):
        return 'FB %s with data %s' % (self.access_token, self.additional_data)

    @classmethod
    def parse_signed_data(cls, signed_request, secret=facebook_settings.FACEBOOK_APP_SECRET):
        '''
        Thanks to 
        http://stackoverflow.com/questions/3302946/how-to-base64-url-decode-in-python
        and
        http://sunilarora.org/parsing-signedrequest-parameter-in-python-bas
        '''
        try:
            l = signed_request.split('.', 2)
            encoded_sig = l[0]
            payload = l[1]
        except IndexError:
            raise ValueError("Signed request malformed")

        sig = base64_url_decode_php_style(encoded_sig)
        data = json.loads(base64_url_decode_php_style(payload))
    
        if data.get('algorithm').upper() != 'HMAC-SHA256':
            logger.error('Signed request is using an unknown algorithm')
            raise ValueError("Signed request is using an unknown algorithm")
        else:
            expected_sig = hmac.new(secret, msg=payload, digestmod=hashlib.sha256).digest()
    
        if sig != expected_sig:
            raise ValueError("Signed request signature mismatch")
        else:
            logger.debug('valid signed request received..')
            return data


    @classmethod
    def generate_signature(cls, postdata, secret=settings.FACEBOOK_APP_SECRET):
        #TODO: is this the same as the cookie based version, if so merge :)
        dlist = sorted(postdata.items(), key=lambda x: x[0])
        signature = hashlib.md5('%s%s' % (''.join(['%s=%s' % (k, v) for k, v in dlist]), secret)).hexdigest()
        return signature

    def is_authenticated(self, raise_=False):
        '''
        Checks if the cookie/post data provided is actually valid
        '''
        if self._is_authenticated is None:
            self._is_authenticated = False
            if self.access_token:
                try:
                    self.facebook_profile_data()
                    self._is_authenticated = True
                except GraphAPIError, e:
                    self._is_authenticated = False
                    if raise_:
                        raise

        return self._is_authenticated


    def get_page(self, raise_=False):
        if 'page' in self.additional_data:
            try:
                self._page = self.additional_data.get('page')
            except GraphAPIError, e:
                self._page = None
                if raise_:
                    raise
                
        return self._page

        
    def get_user(self, raise_=False):
        if 'user' in self.additional_data:
            try:
                self._user = self.additional_data.get('user')
            except GraphAPIError, e:
                self._user = None
                if raise_:
                    raise

        return self._user
        
        
    def is_authorized(self, raise_=False):
        '''
        Checks if the the user has authorized the application
        '''
        if self._is_authorized is None:
            self._is_authorized = False
            try:
                if 'user_id' in self.additional_data:
                    self._user_id = self.additional_data.get('user_id')
                    self._is_authorized = True
            except GraphAPIError, e:
                self._is_authorized = False
                if raise_:
                    raise
                    
        return self._is_authorized

                  
    def pageID(self):
        if self._page is None:
            if self.additional_data:
                try:
                    self.get_page()
                except GraphAPIError, e:
                    self._page = None
                    if raise_:
                        raise

        return self._page['id']


    def userID(self):
        if self._user_id is None:
            if self.additional_data:
                try:
                    self.is_authorized()
                except GraphAPIError, e:
                    self._user_id = None
                    if raise_:
                        raise

        return self._user_id

        
    
    def facebook_profile_data(self):
        '''
        Returns the facebook profile data, together with the image locations
        '''
        if self._profile is None:
            profile = self.get_object('me')
            profile['image'] = 'https://graph.facebook.com/me/picture?type=large&access_token=%s' % self.access_token
            profile['image_thumb'] = 'https://graph.facebook.com/me/picture?access_token=%s' % self.access_token
            self._profile = profile
        return self._profile

        
    def facebook_registration_data(self):
        '''
        Gets all registration data
        and ensures its correct input for a django registration
        '''
        facebook_profile_data = self.facebook_profile_data()
        user_data = {}
        try:
            user_data = FacebookAPI._convert_facebook_data(facebook_profile_data)
        except Exception, e:
            FacebookAPI._report_broken_facebook_data(user_data, facebook_profile_data, e)
            raise

        return user_data

    @classmethod
    def _convert_facebook_data(cls, facebook_profile_data):
        '''
        Takes facebook user data and converts it to a format for usage with Django
        '''
        user_data = facebook_profile_data.copy()
        profile = facebook_profile_data.copy()
        website = profile.get('website')
        if website:
            user_data['website_url'] = cls._extract_url(website)
            
        user_data['facebook_profile_url'] = profile.get('link')
        user_data['facebook_name'] = profile.get('name')
        if len(user_data.get('email', '')) > 75:
            #no more fake email accounts for facebook
            del user_data['email']
        
        gender = profile.get('gender', None)
         
        if gender == 'male':
            user_data['gender'] = 'm'
        elif gender == 'female':
            user_data['gender'] = 'f'

        user_data['username'] = FacebookAPI._retrieve_facebook_username(user_data)
        user_data['password2'] = user_data['password1'] = FacebookAPI._generate_fake_password()

        facebook_map = dict(birthday='date_of_birth', about='about_me', id='facebook_id')
        for k, v in facebook_map.items():
            user_data[v] = user_data.get(k)

        if not user_data['about_me'] and user_data.get('quotes'):
            user_data['about_me'] = user_data.get('quotes')
            
        user_data['date_of_birth'] = FacebookAPI._parse_data_of_birth(user_data['date_of_birth'])
        user_data['username'] = FacebookAPI._create_unique_username(user_data['username'])

        return user_data

    @classmethod
    def _extract_url(cls, text_url_field):
        '''
        >>> url_text = 'http://www.google.com blabla'
        >>> FacebookAPI._extract_url(url_text)
        u'http://www.google.com/'
        
        >>> url_text = 'http://www.google.com/'
        >>> FacebookAPI._extract_url(url_text)
        u'http://www.google.com/'
        
        >>> url_text = 'google.com/'
        >>> FacebookAPI._extract_url(url_text)
        u'http://google.com/'
        
        >>> url_text = 'http://www.fahiolista.com/www.myspace.com/www.google.com'
        >>> FacebookAPI._extract_url(url_text)
        u'http://www.fahiolista.com/www.myspace.com/www.google.com'
        
        >>> url_text = u"""http://fernandaferrervazquez.blogspot.com/\r\nhttp://twitter.com/fferrervazquez\r\nhttp://comunidad.redfashion.es/profile/fernandaferrervazquez\r\nhttp://www.facebook.com/group.php?gid3D40257259997&ref3Dts\r\nhttp://fernandaferrervazquez.spaces.live.com/blog/cns!EDCBAC31EE9D9A0C!326.trak\r\nhttp://www.linkedin.com/myprofile?trk3Dhb_pro\r\nhttp://www.youtube.com/account#profile\r\nhttp://www.flickr.com/\r\n Mi galer\xeda\r\nhttp://www.flickr.com/photos/wwwfernandaferrervazquez-showroomrecoletacom/ \r\n\r\nhttp://www.facebook.com/pages/Buenos-Aires-Argentina/Fernanda-F-Showroom-Recoleta/200218353804?ref3Dts\r\nhttp://fernandaferrervazquez.wordpress.com/wp-admin/"""        
        >>> FacebookAPI._extract_url(url_text)
        u'http://fernandaferrervazquez.blogspot.com/a'
        '''
        import re
        text_url_field = text_url_field.encode('utf8')
        seperation = re.compile('[ ,;\n\r]+')
        parts = seperation.split(text_url_field)
        for part in parts:
            from django.forms import URLField
            url_check = URLField(verify_exists=False)
            try:
                clean_url = url_check.clean(part)
                return clean_url
            except ValidationError, e:
                continue

    @classmethod
    def _generate_fake_password(cls):
        '''
        Returns a random fake password
        '''
        import string
        from random import choice
        size = 9
        password = ''.join([choice(string.letters + string.digits) for i in range(size)])
        return password.lower()


    @classmethod
    def _parse_data_of_birth(cls, data_of_birth_string):
        if data_of_birth_string:
            format = '%m/%d/%Y'
            try:
                parsed_date = datetime.datetime.strptime(data_of_birth_string, format)
                return parsed_date
            except ValueError:
                #Facebook sometimes provides a partial date format ie 04/07 (ignore those)
                if data_of_birth_string.count('/') != 1:
                    raise

    @classmethod
    def _report_broken_facebook_data(cls, facebook_data, original_facebook_data, e):
        '''
        Sends a nice error email with the 
        - facebook data
        - exception
        - stacktrace
        '''
        from pprint import pformat
        data_dump = json.dumps(original_facebook_data)
        data_dump_python = pformat(original_facebook_data)
        message_format = 'The following facebook data failed with error %s\n\n json %s \n\n python %s \n'
        data_tuple = (unicode(e), data_dump, data_dump_python)
        #message = message_format % data_tuple
        #mail_admins('Broken facebook data', message)
        
        logger.error(message_format % data_tuple,
            exc_info=sys.exc_info(), extra={
            'data': {
                 'data_dump': data_dump,
                 'data_dump_python': data_dump_python,
                 'facebook_data': facebook_data,
                 'body': message_format % data_tuple,
             }
        })


    @classmethod
    def _create_unique_username(cls, base_username):
        '''
        Check the database and add numbers to the username to ensure its unique
        '''
        from django.contrib.auth.models import User
        usernames = list(User.objects.filter(username__istartswith=base_username).values_list('username', flat=True))
        usernames_lower = [str(u).lower() for u in usernames]
        username = str(base_username)
        i = 1
        while base_username.lower() in usernames_lower:
            base_username = username + str(i)
            i += 1
        return base_username


    @classmethod
    def _retrieve_facebook_username(cls, facebook_data):
        '''
        Search for the username in 3 places
        - public profile
        - email
        - name
        '''
        link = facebook_data.get('link')
        if link:
            username = link.split('/')[-1]
            username = cls._username_slugify(username)
        if 'profilephp' in username:
            username = None

        if not username and 'email' in facebook_data:
            username = cls._username_slugify(facebook_data.get('email').split('@')[0])
        
        if not username or len(username) < 4:
            username = cls._username_slugify(facebook_data.get('name'))

        return username

    @classmethod
    def _username_slugify(cls, username):
        '''
        Slugify the username and replace - with _ to meet username requirements
        '''
        from django.template.defaultfilters import slugify
        return slugify(username).replace('-', '_')
    
def base64_url_decode_php_style(inp):
    '''
    PHP follows a slightly different protocol for base64 url decode.
    For a full explanation see:
    http://stackoverflow.com/questions/3302946/how-to-base64-url-decode-in-python
    and
    http://sunilarora.org/parsing-signedrequest-parameter-in-python-bas
    '''
    import base64
    padding_factor = (4 - len(inp) % 4) % 4
    inp += "=" * padding_factor 
    return base64.b64decode(unicode(inp).translate(dict(zip(map(ord, u'-_'), u'+/'))))


def get_app_access_token():
    from django_facebook.official_sdk import get_app_access_token
    return get_app_access_token(facebook_settings.FACEBOOK_APP_ID, facebook_settings.FACEBOOK_APP_SECRET)

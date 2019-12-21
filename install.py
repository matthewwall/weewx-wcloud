# $Id: install.py 1799 2019-03-08 11:43:00Z tkeffer $
# installer for WeatherCloud
# Copyright 2014 Matthew Wall

from weecfg.extension import ExtensionInstaller

def loader():
    return WeatherCloudInstaller()

class WeatherCloudInstaller(ExtensionInstaller):
    def __init__(self):
        super(WeatherCloudInstaller, self).__init__(
            version="0.11",
            name='wcloud',
            description='Upload weather data to WeatherCloud.',
            author="Matthew Wall",
            author_email="mwall@users.sourceforge.net",
            restful_services='user.wcloud.WeatherCloud',
            config={
                'StdRESTful': {
                    'WeatherCloud': {
                        'id': 'INSERT_WEATHERCLOUD_ID',
                        'key': 'INSERT_WEATHERCLOUD_KEY'}}},
            files=[('bin/user', ['bin/user/wcloud.py'])]
            )

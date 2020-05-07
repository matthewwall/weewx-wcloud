wcloud - weewx extension that sends data to WeatherCloud
Copyright 2014 Matthew Wall

Installation instructions:

0) download

wget -O weewx-wcloud.zip https://github.com/matthewwall/weewx-wcloud/archive/master.zip

1) run the extension installer:

wee_extension --install weewx-wcloud.zip

2) modify weewx.conf:

[StdRESTful]
    [[WeatherCloud]]
        id = WEATHERCLOUD_ID
        key = WEATHERCLOUD_KEY

3) restart weewx

sudo /etc/init.d/weewx stop
sudo /etc/init.d/weewx start

For configuration options and details, see the comments in wcloud.py

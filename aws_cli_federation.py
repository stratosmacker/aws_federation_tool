#!/usr/bin/python
import sys
import re
import boto.sts
import boto.s3
import requests
import getpass
import argparse
import configparser
import base64
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from os.path import expanduser
from requests_ntlm import HttpNtlmAuth


REGIONS = ["us-east-2", "us-east-1", "us-west-1", "us-west-2", "ca-central-1", "ap-south-1",
           "ap-northeast-2", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
           "eu-central-1", "eu-west-1", "eu-west-2", "sa-east-1"]
##########################################################################
# Variables 
 
PARSER = argparse.ArgumentParser()
PARSER.add_argument('-u', '--username', dest='username', help='Your email')
PARSER.add_argument('-p', '--password', dest='password', help='Your password')
PARSER.add_argument('-r', '--region', dest='region', help='the region string e.g us-west-2')
PARSER.add_argument('-e', '--export', dest='export', const=True, nargs="?", help='Boolean flag, will print export statements to stdout')
PARSER.add_argument('-a', '--account', dest='account', help='Human readable name that for the role you want to assume. It may help to run this script and see what options are available before using the flag')
# region: The default AWS region that this script will connect 
# to for all API calls 
region = '' 

# output format: The AWS CLI output format that will be configured in the
# saml profile (affects subsequent CLI calls)
outputformat = 'json'

# awsconfigfile: The file where this script will store the temp
# credentials under the saml profile
awsconfigfile = '/.aws/credentials'

# SSL certificate verification: Whether or not strict certificate
# verification is done, False should only be used for dev/test
sslverification = True

# idpentryurl: The initial URL that starts the authentication process.
idpentryurl = 'https://fs.swmsp.net/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices'

ARGS = PARSER.parse_args()

##########################################################################

# Get the federated credentials from the user
if not ARGS.username:
    print("Username: ", end='')
    username = input()
else:
    username = ARGS.username

if not ARGS.password:
    password = getpass.getpass()
    print('')
else:
    password = ARGS.password

if not ARGS.region:
    for i,region in enumerate(REGIONS):
        print("[" , i , "] ", region)
    print("Region: ", end='')
    region = REGIONS[int(input())]
    print(region)
else:
    region = ARGS.region

# Initiate session handler 
session = requests.Session() 
 
# Programatically get the SAML assertion 
# Set up the NTLM authentication handler by using the provided credential 
session.auth = HttpNtlmAuth(username, password, session) 
 
# Opens the initial AD FS URL and follows all of the HTTP302 redirects 
response = session.request('GET', idpentryurl, verify=sslverification) 

#determine the Input fields for the POST request
soup = BeautifulSoup(response.text, "html.parser") 
payload = {}
for inputtag in soup.find_all(re.compile('(INPUT|input)')):
    name = inputtag.get('name','')
    value = inputtag.get('value','')
    if "user" in name.lower():
        #Make an educated guess that this is correct field for username
        payload[name] = username
    elif "email" in name.lower():
        #Some IdPs also label the username field as 'email'
        payload[name] = username
    elif "pass" in name.lower():
        #Make an educated guess that this is correct field for password
        payload[name] = password
    else:
        #Populate the parameter with existing value (picks up hidden fields in the login form)
        payload[name] = value

#make post request
response = session.post(
    idpentryurl, data=payload, verify=sslverification)
					 
# Overwrite and delete the credential variables, just for safety
username = '##############################################'
password = '##############################################'
del username
del password

# Look for the SAMLResponse attribute of the input tag (determined by 
# analyzing the debug print lines above) 
assertion = '' 
soup = BeautifulSoup(response.text, "html.parser") 
for inputtag in soup.find_all('input'): 
    if(inputtag.get('name') == 'SAMLResponse'): 
        assertion = inputtag.get('value')

# Parse the returned assertion and extract the authorized roles 
awsroles = [] 
root = None
try:
    root = ET.fromstring(base64.b64decode(assertion))
except: # TODO put a specific exception here 
    print("Error Parsing the SAML response. Please check your credentials. If the problem persists, contact your administrator")
    sys.exit(1)
 
for saml2attribute in root.iter('{urn:oasis:names:tc:SAML:2.0:assertion}Attribute'): 
    if (saml2attribute.get('Name') == 'https://aws.amazon.com/SAML/Attributes/Role'): 
        for saml2attributevalue in saml2attribute.iter('{urn:oasis:names:tc:SAML:2.0:assertion}AttributeValue'):
            awsroles.append(saml2attributevalue.text)
 
# Note the format of the attribute value should be role_arn,principal_arn 
# but lots of blogs list it as principal_arn,role_arn so let's reverse 
# them if needed 
for awsrole in awsroles: 
    chunks = awsrole.split(',') 
    if'saml-provider' in chunks[0]:
        newawsrole = chunks[1] + ',' + chunks[0] 
        index = awsroles.index(awsrole) 
        awsroles.insert(index, newawsrole) 
        awsroles.remove(awsrole)

# If I have more than one role, ask the user which one they want, 
print("")
i = 0 
humannames = []
print("Please choose the role you would like to assume:")
for awsrole in awsroles: 
    humanname = awsrole.split(',')[0].split('/')[1]
    print('[', i, ']: ', awsrole.split(',')[0], " (", humanname, ")")
    humannames.append(humanname)
    i += 1 

print("Selection: ", end="") 
selectedroleindex = input() 

# Basic sanity check of input 
if int(selectedroleindex) > (len(awsroles) - 1): 
    print('You selected an invalid role index, please try again')
    sys.exit(0) 

role_arn = awsroles[int(selectedroleindex)].split(',')[0] 
principal_arn = awsroles[int(selectedroleindex)].split(',')[1]
configname = humannames[int(selectedroleindex)]

# Use the assertion to get an AWS STS token using Assume Role with SAML
conn = boto.sts.connect_to_region(region)
token = conn.assume_role_with_saml(role_arn, principal_arn, assertion)

if not ARGS.export:
    # Write the AWS STS token into the AWS credential file
    home = expanduser("~")
    filename = home + awsconfigfile
     
    # Read in the existing config file
    config = configparser.RawConfigParser()
    config.read(filename)
     
    # Put the credentials into a specific profile instead of clobbering
    # the default credentials
    if not config.has_section(configname):
        config.add_section(configname)
     
    config.set(configname, 'output', outputformat)
    config.set(configname, 'region', region)
    config.set(configname, 'aws_access_key_id', token.credentials.access_key)
    config.set(configname, 'aws_secret_access_key', token.credentials.secret_key)
    config.set(configname, 'aws_session_token', token.credentials.session_token)
     
    # Write the updated config file
    with open(filename, 'w+') as configfile:
        config.write(configfile)

    # Give the user some basic info as to what has just happened
    print('\n\n----------------------------------------------------------------')
    print( 'Your new access key pair has been stored in the AWS configuration file {0} under the {1} profile.'.format(filename, configname))
    print( 'Note that it will expire at {0}.'.format(token.credentials.expiration))
    print( 'After this time you may safely rerun this script to refresh your access key pair.')
    print( 'To use this credential call the AWS CLI with the --profile option (e.g. aws --profile {0} ec2 describe-instances).'.format(configname))
    print( '----------------------------------------------------------------\n\n')
else:
    print( 'export AWS_ACCESS_KEY_ID="{0}"\nexport AWS_SECRET_ACCESS_KEY="{1}"\nexport AWS_SESSION_TOKEN="{2}"'.format(token.credentials.access_key, token.credentials.secret_key, token.credentials.session_token))

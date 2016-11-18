from functools import wraps
from flask import Flask, request, Response, abort, stream_with_context
from datetime import datetime, timedelta
import json
import requests
import os
from requests_ntlm import HttpNtlmAuth
import logging


app = Flask(__name__)
config = {}
config_since = None

logger = None


class DataAccess:
    def __init__(self):
        self._entities = {"users": [], "groups": [], "documents": []}

    def get_entities(self, since, datatype, user, password):
        if not datatype in self._entities:
            abort(404)
        yield "["
        for s in config:
            if  "site-url" in config[s]:
                if since is None:
                    self.get_entitiesdata(config[s], datatype, since, user, password)
                else:
                    [entity for entity in self.get_entitiesdata(config[s], datatype, since) if entity["_updated"] > since]
        yield "]"

    def get_entitiesdata(self, siteconfig, datatype, since, user, password):
        if datatype in self._entities:
            if len(self._entities[datatype]) > 0 and self._entities[datatype][0]["_updated"] > "%sZ" % (datetime.now() - timedelta(hours=12)).isoformat():
                yield json.dumps(self._entities[datatype])
        now = datetime.now()
        start = since
        if since is None:
            start = (now - timedelta(days=5365)).isoformat()
        siteurl = siteconfig["site-url"]
        headers = {'accept': 'application/json;odata=verbose'}
        entities = []
        if datatype == "users":
            logger.info("Reading users from site: %s" % (siteurl))
            r = requests.get(siteurl + "/_api/web/siteusers", auth=HttpNtlmAuth(user, password), headers=headers)
            r.raise_for_status()
            obj = json.loads(r.text)
            logger.debug("Got %s items from user list" % (str(len(obj["d"]["results"]))))

            if "d" in obj:
                entities = obj["d"]["results"]
                for index, e in entities:
                    if index > 0:
                        yield ","
                    e.update({"_id": str(e["Id"])})
                    e.update({"_updated": now.isoformat()})
                    yield json.dumps(e)

        if datatype == "groups":
            logger.info("Reading groups from site: %s" % (siteurl))
            r = requests.get(siteurl + "/_api/web/sitegroups", auth=HttpNtlmAuth(user, password), headers=headers)
            r.raise_for_status()
            obj = json.loads(r.text)
            logger.debug("Got %s items from group list" % (str(len(obj["d"]["results"]))))
            if "d" in obj:
                entities = obj["d"]["results"]
                for index, e in entities:
                    if index > 0:
                        yield ","
                    e.update({"_id": str(e["Id"])})
                    e.update({"_updated": now.isoformat()})
                    logger.debug("Reading group users from: %s" % (e["Users"]["__deferred"]["uri"]))
                    r = requests.get(e["Users"]["__deferred"]["uri"], auth=HttpNtlmAuth(user, password), headers=headers)
                    if r.text:
                        usr = json.loads(r.text)
                        if "d" in usr:
                            logger.debug("Got %s group users" % (str(len(usr["d"]["results"]))))
                            e.update({"users-metadata": usr["d"]["results"]})
                    yield json.dumps(e)

        if datatype == "documents":
            logger.info("Reading documents from site: %s" % (siteurl))

            hura = None
            first = True
            r = None
            if "list-guid" in siteconfig:
                logger.debug("Reading documents using GUID: %s" % (siteconfig["list-guid"]))
                r = requests.get(siteurl + "/_api/web/lists/getbyguid('%s')/items?$filter=Modified ge datetime'%s'" %(siteconfig["list-guid"], start), auth=HttpNtlmAuth(user, password), headers=headers)
                hura = requests.get(siteurl + "/_api/web/lists/getbyguid('%s')/HasUniqueRoleAssignments" %(siteconfig["list-guid"]), auth=HttpNtlmAuth(user, password), headers=headers)
            elif "list-title" in siteconfig:
                logger.debug("Reading documents using title: %s" % (siteconfig["list-title"]))
                r = requests.get(siteurl + "/_api/web/lists/getbytitle('%s')/items?$filter=Modified ge datetime'%s'" %(siteconfig["list-title"], start), auth=HttpNtlmAuth(user, password), headers=headers)
                hura = requests.get(siteurl + "/_api/web/lists/getbytitle('%s')/HasUniqueRoleAssignments" %(siteconfig["list-title"]), auth=HttpNtlmAuth(user, password), headers=headers)

            hasuniqueroleassignments = True
            if hura:
                huraobj = json.loads(hura.text)
                hasuniqueroleassignments = huraobj["d"]["HasUniqueRoleAssignments"]
                logger.debug("Documentlibrary has unique role assignments: %r" % hasuniqueroleassignments)
            next = None
            while True:
                if r:
                    permissions = []
                    firstdocument = True

                    r.raise_for_status()
                    obj = json.loads(r.text)
                    logger.debug("Got %s items from document list" % (str(len(obj["d"]["results"]))))
                    if "__next" in obj["d"]:
                        next = obj["d"]["__next"]
                        logger.debug("There are still more pages..." )
                    if "d" in obj:
                        entities = obj["d"]["results"]
                        for e in entities:
                            if not first:
                                yield ","
                            else:
                                first = False
                            e.update({"_id": str(e["Id"])})
                            e.update({"_updated": str(e["Modified"])})
                            logger.debug("Reading document file from: %s" % (e["File"]["__deferred"]["uri"]))
                            rf = requests.get(e["File"]["__deferred"]["uri"], auth=HttpNtlmAuth(user, password), headers=headers)
                            if rf.text:
                                usr = json.loads(rf.text)
                                if "d" in usr:
                                    e.update({"file-metadata": usr["d"]})
                            if firstdocument | hasuniqueroleassignments:
                                p = requests.get(e["RoleAssignments"]["__deferred"]["uri"], auth=HttpNtlmAuth(user, password), headers=headers)
                                ra = json.loads(p.text)
                                if "d" in ra:
                                    permissions = ra["d"]
                                    firstdocument = False
                            e.update({"file-permissions": permissions})
                            yield json.dumps(e)
                if next:
                    r = requests.get(
                        next, auth=HttpNtlmAuth(user, password), headers=headers)
                    next = None
                else:
                    break
        logger.debug("Adding %s items to result" % (str(len(entities))))
        self._entities[datatype] = entities

data_access_layer = DataAccess()


def authenticate():
    """Sends a 401 response that enables basic auth"""
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth:
            return authenticate()
        return f(*args, **kwargs)
    return decorated


def read_config(config_url):
    global config_since
    global config
    parameter = "?history=false"
    if config_since:
        parameter = parameter + "&since=%s" %(str(config_since))

    logger.info("Reading config dataset from %s" % (config_url + parameter))
    r = requests.get(config_url + parameter)
    r.raise_for_status()
    logger.debug("Reading config from %s: %s" % (config_url + parameter, r.text))
    change = json.loads(r.text)
    for changed_item in change:
        changed_item_id = changed_item["_id"]
        if changed_item["_deleted"]:
            logger.debug("Deletes _id %s" % (changed_item["_id"]))
            if changed_item_id in config:
                del config[changed_item_id]
        else:
            logger.debug("Updates _id %s with: %s" % (changed_item["_id"], changed_item))
            config[changed_item_id] = changed_item
        changed_item_updated = changed_item["_updated"]
        if config_since is None or changed_item_updated > config_since:
            config_since = changed_item_updated


@app.route('/<datatype>')
@requires_auth
def get_entities(datatype):
    logger.info("Get %s using request: %s" % (datatype, request.url))
    since = request.args.get('since')
    conf = None
    if 'CONFIG_DATASET' in os.environ:
        conf = os.environ['CONFIG_DATASET']
    if not conf:
        conf = request.args.get('config-dataset')
    if conf:
        read_config(conf)
    auth = request.authorization
    #entities = data_access_layer.get_entities(since, datatype, auth.username, auth.password)
    #return Response(json.dumps(entities), mimetype='application/json')

    # Generate the response
    try:
        return Response(stream_with_context(data_access_layer.get_entities(since, datatype, auth.username, auth.password)), mimetype='application/json')
    except BaseException as e:
        return Response(status=500, response="An error occured during transform of input")

if __name__ == '__main__':
    # Set up logging
    format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logger = logging.getLogger('sharepoint-microservice')

    # Log to stdout
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(logging.Formatter(format_string))
    logger.addHandler(stdout_handler)

    logger.setLevel(logging.DEBUG)

    app.run(debug=True, host='0.0.0.0')


"""Primitive candidate API observations; no assertions, expected values or grades."""

from __future__ import annotations

import json
import sys


def blueprint(flask, args):
    instance = flask.Blueprint(args["name"], __name__)
    return {"name": instance.name}


def redirect(flask, args):
    app = flask.Flask(__name__)
    if args["override"]:

        class Application(flask.Flask):
            def redirect(self, location, code=302):
                return self.response_class(
                    args["body"],
                    status=code,
                    headers={"Location": location, "X-Observation": args["override"]},
                )

        app = Application(__name__)

    def invoke():
        function = app.redirect if args["call"] == "method" else flask.redirect
        response = function(args["location"], code=args["code"])
        return {
            "status": response.status_code,
            "location": response.headers.get("Location"),
            "override": response.headers.get("X-Observation"),
            "body": response.get_data(as_text=True) if args["override"] else None,
        }

    if args["context"]:
        with app.app_context():
            return invoke()
    return invoke()


def routes(flask, args):
    app = flask.Flask(__name__, subdomain_matching=True)
    app.config["SERVER_NAME"] = "factory.test"
    parent = flask.Blueprint("parent", __name__, subdomain=args["parent"])
    child = flask.Blueprint("child", __name__, subdomain=args["child"])
    child.add_url_rule(args["path"], endpoint="leaf", view_func=lambda: args["body"])
    if args["nested"]:
        parent.register_blueprint(child, url_prefix=args["prefix"])
        options = {"subdomain": args["registration"]} if args["registration"] else {}
        app.register_blueprint(parent, **options)
        endpoint = "parent.child.leaf"
    else:
        app.register_blueprint(child)
        endpoint = "child.leaf"
    with app.app_context():
        url = flask.url_for(endpoint, _external=True)
    response = app.test_client().get(args["request_path"], base_url="http://" + args["host"])
    return {"url": url, "status": response.status_code, "body": response.get_data(as_text=True)}


def main():
    requests = json.loads(sys.stdin.buffer.read(65537))
    if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
        raise ValueError("Observation request budget exceeded")
    import flask

    if flask.__file__ != "/workspace/src/flask/__init__.py":
        raise ValueError("Candidate import did not use the task source")
    operations = {"blueprint": blueprint, "redirect": redirect, "routes": routes}
    rows = []
    for request in requests:
        try:
            value = operations[request["op"]](flask, request["args"])
        except Exception as error:  # noqa: BLE001 — API exceptions are raw observations, never verdicts.
            value = {"error": type(error).__name__}
        rows.append({"id": request["id"], "value": value})
    sys.stdout.write(json.dumps(rows, allow_nan=False))


if __name__ == "__main__":
    main()

NAMESPACE ?= dso202-assignment-02
CLUSTER   ?= dso202
HOST      ?= tasktracker.local
OPERATOR_IMAGE ?= dso202/taskbackup-operator:0.1.2
K := kubectl -n $(NAMESPACE)

.PHONY: all tls ingress-mesh app istio operator-image operator backup apply verify rbac-check cleanup

all: apply

# Self-signed certificate for the Ingress (2.2). Key never lands in the repo.
tls:
	@if $(K) get secret tasktracker-tls >/dev/null 2>&1; then echo "secret/tasktracker-tls exists"; else \
	  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
	    -keyout /tmp/tasktracker.key -out /tmp/tasktracker.crt \
	    -subj "/CN=$(HOST)/O=DSO202" -addext "subjectAltName=DNS:$(HOST)" && \
	  $(K) create secret tls tasktracker-tls --cert=/tmp/tasktracker.crt --key=/tmp/tasktracker.key && \
	  rm -f /tmp/tasktracker.key /tmp/tasktracker.crt; fi

# Put ingress-nginx into the mesh so it can reach the STRICT-mTLS namespace (2.5).
ingress-mesh:
	kubectl patch deployment ingress-nginx-controller -n ingress-nginx --patch-file ingress-nginx-istio-patch.yaml
	kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=180s

app:
	kubectl apply -f namespace.yaml
	kubectl apply -f configmap.yaml -f secret.yaml -f rbac.yaml
	$(MAKE) tls
	kubectl apply -f database/statefulset.yaml
	$(K) rollout status statefulset/db --timeout=300s
	kubectl apply -f backend/deployment.yaml -f frontend/deployment.yaml
	$(K) rollout status deployment/backend --timeout=300s
	$(K) rollout status deployment/frontend --timeout=300s
	kubectl apply -f ingress.yaml

istio:
	kubectl apply -f istio.yaml

operator-image:
	docker build -t $(OPERATOR_IMAGE) operator
	kind load docker-image $(OPERATOR_IMAGE) --name $(CLUSTER)

operator:
	kubectl apply -f operator/crd.yaml
	kubectl wait --for=condition=established crd/taskbackups.dso202.cst.edu.bt --timeout=60s
	kubectl apply -f operator/rbac.yaml -f operator/deployment.yaml
	kubectl rollout status deployment/taskbackup-operator -n taskbackup-operator --timeout=180s

backup:
	kubectl apply -f operator/sample-taskbackup.yaml
	$(K) wait taskbackup/nightly-001 --for=jsonpath='{.status.phase}'=Succeeded --timeout=180s
	$(K) get taskbackup
	$(K) logs job/nightly-001-dump -c pg-dump

apply: app istio operator

verify:
	$(K) get statefulset,deployment,pod,svc,pvc,ingress
	$(K) get peerauthentication,authorizationpolicy,destinationrule,virtualservice
	$(K) get taskbackup
	curl -sk --resolve $(HOST):8443:127.0.0.1 https://$(HOST):8443/backend/api/status || \
	  echo "(run: kubectl port-forward -n ingress-nginx svc/ingress-nginx-controller 8443:443)"

# Each line prints yes/no; see README section 2.3 for the expected answers.
rbac-check:
	@for c in "list pods --as=system:serviceaccount:$(NAMESPACE):backend-sa" \
	          "patch deployment/backend --as=system:serviceaccount:$(NAMESPACE):ci-deployer" \
	          "patch deployment/other --as=system:serviceaccount:$(NAMESPACE):ci-deployer" \
	          "delete deployment/backend --as=system:serviceaccount:$(NAMESPACE):ci-deployer" \
	          "create pods --subresource=exec --as=dev@cst.edu.bt --as-group=dso202-developers" \
	          "get secrets --as=dev@cst.edu.bt --as-group=dso202-developers" \
	          "create taskbackups.dso202.cst.edu.bt --as=dev@cst.edu.bt --as-group=dso202-developers" \
	          "list pods --as=auditor@cst.edu.bt" \
	          "get secrets --as=auditor@cst.edu.bt" \
	          "create jobs --as=system:serviceaccount:taskbackup-operator:taskbackup-operator" \
	          "delete jobs --as=system:serviceaccount:taskbackup-operator:taskbackup-operator"; do \
	  printf '%-4s %s\n' "$$(kubectl auth can-i $$c -n $(NAMESPACE) 2>/dev/null)" "$$c"; done
	@printf '%-4s %s\n' "$$(kubectl auth can-i list pods --as=auditor@cst.edu.bt -n kube-system 2>/dev/null)" "list pods --as=auditor@cst.edu.bt -n kube-system"

# PVCs are retained by design (whenDeleted: Retain); deleting the namespace removes them.
cleanup:
	-kubectl delete -f operator/deployment.yaml -f operator/rbac.yaml --ignore-not-found
	-kubectl delete -f operator/crd.yaml --ignore-not-found
	kubectl delete namespace $(NAMESPACE) --ignore-not-found

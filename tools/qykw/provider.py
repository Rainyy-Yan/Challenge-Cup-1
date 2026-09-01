"""Fail-closed, transport-injectable inference provider for qykw."""
from __future__ import annotations
import http.client, ipaddress, json, os, socket, ssl, time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from http.client import IncompleteRead
from typing import Protocol
from urllib.parse import urlsplit
from tools.qykw.domain import InferenceError, InferenceErrorCode, InferenceFailure, InferenceRequest, InferenceResponse, InferenceUsage, ProviderCapabilities

_REQUIRED_ENVIRONMENT = ("QYKW_INFERENCE_API_KEY", "QYKW_INFERENCE_BASE_URL", "QYKW_INFERENCE_MODEL", "QYKW_INFERENCE_ALLOWED_HOSTS", "QYKW_INFERENCE_CONTEXT_WINDOW", "QYKW_INFERENCE_MAX_OUTPUT_TOKENS", "QYKW_INFERENCE_TIMEOUT_SECONDS")
_MAX_RESPONSE_BODY_BYTES = 1_048_576
_RETRY_DELAY_SECONDS = 0.1
_SAFE_REQUEST_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

class InferenceProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def complete(self, request: InferenceRequest) -> InferenceResponse: ...

class ProviderErrorCode(str, Enum):
    INVALID_CONFIG="invalid_config"; ENDPOINT_INVALID="endpoint_invalid"; ENDPOINT_BLOCKED="endpoint_blocked"; ENDPOINT_NOT_ALLOWED="endpoint_not_allowed"; ENDPOINT_REDIRECT_REJECTED="endpoint_redirect_rejected"; DNS_ERROR="dns_error"; TLS_ERROR="tls_error"; CONNECTION_ERROR="connection_error"; READ_TIMEOUT="read_timeout"; RATE_LIMITED="rate_limited"; RESPONSE_INTERRUPTED="response_interrupted"; INVALID_RESPONSE="invalid_response"; DEADLINE_EXCEEDED="deadline_exceeded"

class ProviderError(RuntimeError):
    """Content-free error: its args and repr contain only the generic code."""
    def __init__(self, code: ProviderErrorCode) -> None:
        super().__init__(code.value); self.code = code

class TransportFailureKind(str, Enum):
    DNS="dns"; TLS_HANDSHAKE="tls_handshake"; CERTIFICATE="certificate"; CONNECTION="connection"; READ_TIMEOUT="read_timeout"; RESPONSE_INTERRUPTED="response_interrupted"

class TransportFailure(Exception):
    """Transport result; raw detail is deliberately omitted from Exception.args."""
    def __init__(self, kind: TransportFailureKind, *, pre_send: bool=False, detail: str="") -> None:
        # ``detail`` is accepted for adapter compatibility but deliberately
        # discarded so a caught failure cannot retain request/response text.
        del detail
        super().__init__(kind.value); self.kind=kind; self.pre_send=pre_send

@dataclass(frozen=True)
class TransportRequest:
    method: str; url: str; host: str; port: int; resolved_ip: str; headers: Mapping[str,str]; body: bytes; timeout_seconds: int
@dataclass(frozen=True)
class TransportResponse:
    status: int; headers: Mapping[str,str]; body: bytes
class InferenceTransport(Protocol):
    def send(self, request: TransportRequest) -> TransportResponse: ...

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, resolved_ip: str, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context()); self._resolved_ip=resolved_ip
    def connect(self) -> None:
        raw_socket=socket.create_connection((self._resolved_ip, self.port), self.timeout)
        try: self.sock=self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException: raw_socket.close(); raise

class StdlibHTTPSInferenceTransport:
    """Pinned HTTPS transport; it never follows redirects."""
    def send(self, request: TransportRequest) -> TransportResponse:
        parsed=urlsplit(request.url); connection=_PinnedHTTPSConnection(request.host, request.port, request.resolved_ip, request.timeout_seconds)
        try:
            try: connection.connect()
            except socket.gaierror as exc: raise TransportFailure(TransportFailureKind.DNS, pre_send=True) from exc
            except ssl.SSLCertVerificationError as exc: raise TransportFailure(TransportFailureKind.CERTIFICATE, pre_send=True) from exc
            except ssl.SSLError as exc: raise TransportFailure(TransportFailureKind.TLS_HANDSHAKE, pre_send=True) from exc
            except (OSError, TimeoutError) as exc: raise TransportFailure(TransportFailureKind.CONNECTION, pre_send=True) from exc
            try: connection.request(request.method, parsed.path or "/", body=request.body, headers=dict(request.headers))
            except (socket.timeout, TimeoutError) as exc: raise TransportFailure(TransportFailureKind.READ_TIMEOUT) from exc
            except (OSError, ConnectionError) as exc: raise TransportFailure(TransportFailureKind.CONNECTION, pre_send=False) from exc
            try: response=connection.getresponse(); body=response.read(_MAX_RESPONSE_BODY_BYTES+1)
            except (socket.timeout, TimeoutError) as exc: raise TransportFailure(TransportFailureKind.READ_TIMEOUT) from exc
            except (IncompleteRead, http.client.HTTPException, OSError, ConnectionError) as exc: raise TransportFailure(TransportFailureKind.RESPONSE_INTERRUPTED) from exc
            return TransportResponse(response.status, dict(response.headers.items()), body)
        finally: connection.close()

def validate_provider_capabilities(provider: InferenceProvider, request: InferenceRequest) -> None:
    capabilities=provider.capabilities(); required=request.max_output_tokens+estimate_request_input_tokens(request)
    if not (request.reasoning_profile=="maximum" and "maximum" in capabilities.supported_reasoning_profiles and capabilities.structured_output and capabilities.context_window>=required and capabilities.max_output_tokens>=request.max_output_tokens>0):
        raise InferenceError(InferenceFailure(InferenceErrorCode.CAPABILITY_UNSUPPORTED, False, False))

def estimate_request_input_tokens(request: InferenceRequest) -> int:
    envelope={"run_id":request.run_id,"stage":request.stage.value,"prompt_version":request.prompt_version,"reasoning_profile":request.reasoning_profile,"deadline_seconds":request.deadline_seconds,"max_output_tokens":request.max_output_tokens,"idempotency_key":request.idempotency_key,"schema_name":request.schema_name,"schema":request.schema,"payload":request.payload}
    return max(1,len(json.dumps(envelope,ensure_ascii=False,separators=(",",":"),sort_keys=True).encode("utf-8")))

class ResponsesInferenceProvider:
    """HTTPS-only adapter with DNS pinning, bounded retry, and safe telemetry."""
    def __init__(self, *, api_key: str, base_url: str, model: str, allowed_hosts: Sequence[str], context_window: int, max_output_tokens: int, timeout_seconds: int, transport: InferenceTransport|None=None, dns_resolver: Callable[[str,int],Sequence[str]]|None=None, clock: Callable[[],float]=time.monotonic, sleep: Callable[[float],None]=time.sleep, logger: Callable[[Mapping[str,object]],None]|None=None) -> None:
        self._api_key=api_key; self._base_url=base_url; self._model=model; self._allowed_hosts=tuple(_canonical_host(host) for host in allowed_hosts); self._context_window=context_window; self._max_output_tokens=max_output_tokens; self._timeout_seconds=timeout_seconds; self._transport=transport or StdlibHTTPSInferenceTransport(); self._dns_resolver=dns_resolver or _stdlib_dns_resolver; self._clock=clock; self._sleep=sleep; self._logger=logger
    @classmethod
    def from_env(cls) -> "ResponsesInferenceProvider":
        values={name:os.environ.get(name) for name in _REQUIRED_ENVIRONMENT}
        if any(not value for value in values.values()): raise ProviderError(ProviderErrorCode.INVALID_CONFIG)
        try:
            context=_bounded_int(values["QYKW_INFERENCE_CONTEXT_WINDOW"],1,2_000_000); output=_bounded_int(values["QYKW_INFERENCE_MAX_OUTPUT_TOKENS"],1,context); timeout=_bounded_int(values["QYKW_INFERENCE_TIMEOUT_SECONDS"],1,3600); hosts=tuple(p.strip() for p in values["QYKW_INFERENCE_ALLOWED_HOSTS"].split(","))
            if not hosts or any(not host for host in hosts): raise ValueError
            return cls(api_key=values["QYKW_INFERENCE_API_KEY"],base_url=values["QYKW_INFERENCE_BASE_URL"],model=values["QYKW_INFERENCE_MODEL"],allowed_hosts=hosts,context_window=context,max_output_tokens=output,timeout_seconds=timeout)
        except (TypeError,ValueError,UnicodeError): raise ProviderError(ProviderErrorCode.INVALID_CONFIG) from None
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(self._context_window,self._max_output_tokens,True,frozenset({"maximum"}))
    def complete(self, request: InferenceRequest) -> InferenceResponse:
        validate_provider_capabilities(self,request)
        try:
            _validate_schema_contract(request.schema)
            body = _request_body(self._model, request)
            if len(body) + request.max_output_tokens > self._context_window:
                raise ValueError
        except ProviderError:
            raise
        except (TypeError, ValueError, UnicodeError):
            raise ProviderError(ProviderErrorCode.INVALID_CONFIG) from None
        endpoint=_validate_endpoint(self._base_url,self._allowed_hosts); started=self._clock(); calls=0
        for attempt in range(2):
            if self._clock()-started>=request.deadline_seconds: self._fail(request,calls,ProviderErrorCode.DEADLINE_EXCEEDED)
            try:
                resolved_ip=_resolve_public(endpoint.host,endpoint.port,self._dns_resolver); calls+=1
                response=self._transport.send(TransportRequest("POST",self._base_url,endpoint.host,endpoint.port,resolved_ip,{"accept":"application/json","authorization":f"Bearer {self._api_key}","content-type":"application/json","idempotency-key":request.idempotency_key},body,self._timeout_seconds))
            except BaseException as exc:
                failure=_transport_failure(exc)
                if failure is None: self._fail(request,calls,ProviderErrorCode.CONNECTION_ERROR)
                code,retry=failure
                if retry and attempt==0 and self._within_deadline(started,request.deadline_seconds,_RETRY_DELAY_SECONDS): self._sleep(_RETRY_DELAY_SECONDS); continue
                self._fail(request,calls,code)
            if 300<=response.status<400: self._fail(request,calls,ProviderErrorCode.ENDPOINT_REDIRECT_REJECTED)
            if response.status==429:
                delay=_retry_after(response.headers)
                if attempt==0 and delay is not None and self._within_deadline(started,request.deadline_seconds,delay): self._sleep(delay); continue
                self._fail(request,calls,ProviderErrorCode.RATE_LIMITED)
            if response.status!=200: self._fail(request,calls,ProviderErrorCode.INVALID_RESPONSE)
            try: parsed=_parse_response(response,request.schema,request.max_output_tokens,self._context_window)
            except (TypeError,ValueError,UnicodeDecodeError,json.JSONDecodeError): self._fail(request,calls,ProviderErrorCode.INVALID_RESPONSE)
            self._emit(run_id=request.run_id,stage=request.stage.value,request_id=parsed.request_id,elapsed=max(0.0,self._clock()-started),call_count=calls,token_usage={"input_tokens":parsed.usage.input_tokens,"output_tokens":parsed.usage.output_tokens})
            return parsed
        self._fail(request,calls,ProviderErrorCode.CONNECTION_ERROR)
    def _within_deadline(self, started: float, deadline: int, delay: float) -> bool: return self._clock()-started+delay<deadline
    def _fail(self, request: InferenceRequest, calls: int, code: ProviderErrorCode) -> None:
        self._emit(run_id=request.run_id,stage=request.stage.value,call_count=calls,error_code=code.value); raise ProviderError(code) from None
    def _emit(self, **fields: object) -> None:
        if self._logger is not None: self._logger(fields)

@dataclass(frozen=True)
class _Endpoint: host: str; port: int
def _bounded_int(value: str|None, low: int, high: int) -> int:
    if value is None or not value.isascii() or not value.isdecimal(): raise ValueError
    result=int(value)
    if not low<=result<=high: raise ValueError
    return result
def _canonical_host(host: str) -> str:
    if not isinstance(host,str) or not host or host!=host.strip() or any(ord(c)>127 for c in host): raise ValueError
    candidate=host[:-1] if host.endswith(".") else host
    if not candidate or candidate.lower()!=candidate or ".." in candidate: raise ValueError
    encoded=candidate.encode("idna").decode("ascii")
    if encoded!=candidate or any(not(part and part.replace("-","").isalnum()) for part in candidate.split(".")): raise ValueError
    return candidate
def _validate_endpoint(url: str, allowed_hosts: tuple[str,...]) -> _Endpoint:
    try:
        parsed=urlsplit(url)
        if parsed.scheme!="https" or not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or not parsed.path.startswith("/"): raise ValueError
        host=parsed.hostname
        if host is None: raise ValueError
        try: ipaddress.ip_address(host)
        except ValueError: pass
        else: raise ProviderError(ProviderErrorCode.ENDPOINT_BLOCKED)
        canonical=_canonical_host(host)
        if parsed.port not in (None,443): raise ValueError
    except (ValueError,UnicodeError): raise ProviderError(ProviderErrorCode.ENDPOINT_INVALID) from None
    if canonical not in allowed_hosts: raise ProviderError(ProviderErrorCode.ENDPOINT_NOT_ALLOWED)
    return _Endpoint(canonical,443)
def _stdlib_dns_resolver(host: str, port: int) -> Sequence[str]: return tuple(record[4][0] for record in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM))
def _resolve_public(host: str, port: int, resolver: Callable[[str,int],Sequence[str]]) -> str:
    try: addresses=tuple(resolver(host,port))
    except (socket.gaierror,OSError,ValueError) as exc: raise TransportFailure(TransportFailureKind.DNS,pre_send=True) from exc
    if not addresses: raise TransportFailure(TransportFailureKind.DNS,pre_send=True)
    parsed=[]
    for address in addresses:
        try: candidate=ipaddress.ip_address(address)
        except ValueError as exc: raise TransportFailure(TransportFailureKind.DNS,pre_send=True) from exc
        if not _is_acceptable_global_address(candidate): raise ProviderError(ProviderErrorCode.ENDPOINT_BLOCKED)
        parsed.append(candidate)
    return str(parsed[0])
def _request_body(model: str, request: InferenceRequest) -> bytes:
    body=json.dumps({"model":model,"input":request.payload,"reasoning":{"effort":request.reasoning_profile},"max_output_tokens":request.max_output_tokens,"response_format":{"type":"json_schema","name":request.schema_name,"strict":True,"schema":request.schema}},ensure_ascii=False,separators=(",",":")).encode("utf-8")
    if len(body)>_MAX_RESPONSE_BODY_BYTES: raise ProviderError(ProviderErrorCode.INVALID_CONFIG)
    return body
def _is_acceptable_global_address(candidate: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return candidate.is_global and not any((candidate.is_private, candidate.is_loopback, candidate.is_link_local, candidate.is_multicast, candidate.is_reserved, candidate.is_unspecified, getattr(candidate, "is_site_local", False)))
def _validate_schema_contract(schema: Mapping[str, object], *, root: bool=True) -> None:
    if not isinstance(schema, Mapping): raise ValueError
    expected=schema.get("type")
    if root and expected != "object": raise ValueError
    common={"type", "description"}
    if root: common.add("title")
    if expected=="object":
        allowed=common|{"additionalProperties", "required", "properties"}
        properties=schema.get("properties"); required=schema.get("required")
        if set(schema)-allowed or schema.get("additionalProperties") is not False or not isinstance(properties,Mapping) or not isinstance(required,list) or len(required)!=len(set(required)) or set(required)!=set(properties) or not all(isinstance(name,str) for name in required): raise ValueError
        for child in properties.values(): _validate_schema_contract(child,root=False)
    elif expected=="array":
        if set(schema) - (common | {"items"}): raise ValueError
        items=schema.get("items")
        if not isinstance(items,Mapping): raise ValueError
        _validate_schema_contract(items,root=False)
    elif expected=="string":
        allowed=common|{"minLength", "enum"}
        if set(schema)-allowed: raise ValueError
        minimum=schema.get("minLength")
        enum=schema.get("enum")
        if minimum is not None and (not isinstance(minimum,int) or isinstance(minimum,bool) or minimum<0): raise ValueError
        if enum is not None and (not isinstance(enum,list) or not enum or not all(isinstance(value,str) for value in enum)): raise ValueError
    elif expected=="integer":
        allowed=common|{"minimum"}
        if set(schema)-allowed: raise ValueError
        minimum=schema.get("minimum")
        if minimum is not None and (not isinstance(minimum,int) or isinstance(minimum,bool)): raise ValueError
    else: raise ValueError
def _transport_failure(exc: BaseException) -> tuple[ProviderErrorCode,bool]|None:
    if isinstance(exc,ProviderError): raise exc
    if not isinstance(exc,TransportFailure): return None
    if exc.kind is TransportFailureKind.DNS: return ProviderErrorCode.DNS_ERROR,True
    if exc.kind is TransportFailureKind.TLS_HANDSHAKE: return ProviderErrorCode.TLS_ERROR,True
    if exc.kind is TransportFailureKind.CERTIFICATE: return ProviderErrorCode.TLS_ERROR,False
    if exc.kind is TransportFailureKind.READ_TIMEOUT: return ProviderErrorCode.READ_TIMEOUT,False
    if exc.kind is TransportFailureKind.RESPONSE_INTERRUPTED: return ProviderErrorCode.RESPONSE_INTERRUPTED,False
    return ProviderErrorCode.CONNECTION_ERROR,exc.pre_send
def _retry_after(headers: Mapping[str,str]) -> float|None:
    value=next((v for k,v in headers.items() if k.lower()=="retry-after"),None)
    try: delay=float(value)
    except (TypeError,ValueError): return None
    return delay if 0<delay<=60 else None
def _parse_response(response: TransportResponse, schema: Mapping[str,object], max_output_tokens: int, context_window: int) -> InferenceResponse:
    content_type=next((v for k,v in response.headers.items() if k.lower()=="content-type"),"")
    if "application/json" not in content_type.lower() or len(response.body)>_MAX_RESPONSE_BODY_BYTES: raise ValueError
    document=json.loads(response.body.decode("utf-8"))
    if not isinstance(document,dict) or set(document)!={"id","output","usage"}: raise ValueError
    request_id,output,usage=document["id"],document["output"],document["usage"]
    if not isinstance(request_id,str) or not request_id or len(request_id)>128 or not set(request_id)<=_SAFE_REQUEST_ID: raise ValueError
    if not isinstance(output,dict) or set(output)!={"value"} or not isinstance(output["value"],dict): raise ValueError
    if not isinstance(usage,dict) or set(usage)!={"input_tokens","output_tokens"}: raise ValueError
    for count in usage.values():
        if count is not None and (not isinstance(count,int) or isinstance(count,bool) or count<0): raise ValueError
    input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
    if output_tokens is not None and output_tokens > max_output_tokens: raise ValueError
    if input_tokens is not None and input_tokens > context_window: raise ValueError
    if input_tokens is not None and output_tokens is not None and input_tokens + output_tokens > context_window: raise ValueError
    _validate_schema(output["value"],schema)
    return InferenceResponse(request_id,output["value"],InferenceUsage(usage["input_tokens"],usage["output_tokens"]))
def _validate_schema(value: object, schema: Mapping[str,object]) -> None:
    if not isinstance(schema,Mapping): raise ValueError
    expected=schema.get("type")
    if expected=="object":
        properties=schema.get("properties"); required=schema.get("required")
        if not isinstance(value,dict) or not isinstance(properties,Mapping) or not isinstance(required,list) or not all(isinstance(n,str) for n in required): raise ValueError
        if schema.get("additionalProperties") is False and set(value)-set(properties): raise ValueError
        if any(n not in value for n in required): raise ValueError
        for name,item in value.items():
            if name not in properties or not isinstance(properties[name],Mapping): raise ValueError
            _validate_schema(item,properties[name])
    elif expected=="array":
        if not isinstance(value,list) or not isinstance(schema.get("items"),Mapping): raise ValueError
        for item in value: _validate_schema(item,schema["items"])
    elif expected=="string":
        if not isinstance(value,str) or len(value)<schema.get("minLength",0) or ("enum" in schema and value not in schema["enum"]): raise ValueError
    elif expected=="integer":
        if not isinstance(value,int) or isinstance(value,bool) or value<schema.get("minimum",-(2**63)): raise ValueError
    else: raise ValueError

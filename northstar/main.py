from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.formparsers import MultiPartParser
from starlette.middleware.sessions import SessionMiddleware

from northstar import __version__
from northstar.auth import (
    MAX_OAUTH_CALLBACK_QUERY_BYTES,
    IdentityMiddleware,
    clear_oauth_session,
    csrf_token,
    oauth_session_binding,
    require_csrf,
)
from northstar.config import Settings
from northstar.data import (
    MAX_BITCOIN_BANDS,
    BitcoinContributionBand,
    CashRoleSetting,
    Database,
    EnvelopeSetting,
    GuidanceRepository,
    HouseholdPolicyRepository,
    HouseholdTarget,
    OAuthStateError,
    PortfolioRepository,
    PortfolioRules,
    ProviderConnectionRepository,
    ProviderConnectionStateError,
    SecurityPolicyMapping,
    SinkingFundSetting,
    TargetSetting,
)
from northstar.domain import bitcoin_guardrail
from northstar.imports import MAX_FILE_BYTES, ImportValidationError, ValidationIssue
from northstar.integrations import (
    HttpSchwabOAuthClient,
    HttpSchwabReadClient,
    SchwabOAuthClient,
    SchwabOAuthError,
    SchwabReadClient,
)
from northstar.services import (
    BackupRetentionPolicy,
    BackupService,
    DiscoveryError,
    GuidanceService,
    HouseholdPlanService,
    PortfolioImportService,
    PreviewSession,
    SchwabDiscoveryService,
    SchwabSnapshotCoordinator,
    SchwabSnapshotService,
    format_money,
    format_percentage,
    format_quantity,
)
from northstar.web.security import (
    MAX_IMPORT_REQUEST_BYTES,
    ImportRequestLimitMiddleware,
    OriginBoundaryMiddleware,
    SecurityHeadersMiddleware,
)

PACKAGE_DIR = Path(__file__).parent
HOUSEHOLD_TARGET_SLEEVES = (
    "us_equity",
    "international_equity",
    "nominal_treasury",
    "tips",
    "investable_cash",
    "bitcoin",
)
HOUSEHOLD_POLICY_SLEEVES = HOUSEHOLD_TARGET_SLEEVES + ("unclassified",)


def create_app(
    settings: Settings | None = None,
    *,
    access_verifier: object | None = None,
    schwab_oauth_client: SchwabOAuthClient | None = None,
    schwab_read_client: SchwabReadClient | None = None,
) -> FastAPI:
    active_settings = settings or Settings.from_env()
    active_settings.validate()
    database = Database(
        active_settings.database_path,
        active_settings.data_vault(),
        active_settings.database_key,
    )
    backup_service = BackupService(
        database,
        active_settings.backup_dir,
        active_settings.backup_vault(),
        BackupRetentionPolicy(
            active_settings.backup_daily_retention,
            active_settings.backup_monthly_retention,
        ),
    )
    portfolio = PortfolioRepository(database)
    guidance_repository = GuidanceRepository(database)
    guidance_service = GuidanceService(guidance_repository, portfolio)
    household_policy_repository = HouseholdPolicyRepository(database)
    household_plan_service = HouseholdPlanService(
        household_policy_repository, guidance_repository, guidance_service
    )
    import_service = PortfolioImportService(database, portfolio)
    provider_connections = ProviderConnectionRepository(database)
    owned_schwab_oauth_client: HttpSchwabOAuthClient | None = None
    owned_schwab_read_client: HttpSchwabReadClient | None = None
    active_schwab_oauth_client = schwab_oauth_client
    if active_settings.schwab_enabled and active_schwab_oauth_client is None:
        secret_value = active_settings.schwab_client_secret or ""
        owned_schwab_oauth_client = HttpSchwabOAuthClient(
            client_id=active_settings.schwab_client_id or "",
            client_secret=secret_value,
            redirect_uri=active_settings.schwab_redirect_uri or "",
        )
        active_schwab_oauth_client = owned_schwab_oauth_client
    active_schwab_read_client = schwab_read_client
    if active_settings.schwab_enabled and active_schwab_read_client is None:
        owned_schwab_read_client = HttpSchwabReadClient()
        active_schwab_read_client = owned_schwab_read_client
    schwab_discovery = SchwabDiscoveryService() if active_schwab_read_client is not None else None
    schwab_snapshot_service = SchwabSnapshotService(database, provider_connections)
    schwab_snapshot_coordinator = (
        SchwabSnapshotCoordinator(
            connections=provider_connections,
            oauth_client=active_schwab_oauth_client,
            read_client=active_schwab_read_client,
            snapshots=schwab_snapshot_service,
        )
        if active_schwab_oauth_client is not None and active_schwab_read_client is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        active_settings.validate_runtime_permissions()
        database.initialize()
        import_service.purge_expired_previews()
        try:
            yield
        finally:
            if owned_schwab_oauth_client is not None:
                await owned_schwab_oauth_client.close()
            if owned_schwab_read_client is not None:
                await owned_schwab_read_client.close()

    app = FastAPI(
        title="Northstar Local",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.database = database
    app.state.backup_service = backup_service
    app.state.portfolio = portfolio
    app.state.guidance_repository = guidance_repository
    app.state.guidance_service = guidance_service
    app.state.household_policy_repository = household_policy_repository
    app.state.household_plan_service = household_plan_service
    app.state.import_service = import_service
    app.state.provider_connections = provider_connections
    app.state.schwab_oauth_client = active_schwab_oauth_client
    app.state.schwab_discovery = schwab_discovery
    app.state.schwab_snapshot_coordinator = schwab_snapshot_coordinator

    MultiPartParser.spool_max_size = MAX_IMPORT_REQUEST_BYTES + 1
    app.add_middleware(
        IdentityMiddleware,
        settings=active_settings,
        verifier=access_verifier,
    )
    app.add_middleware(ImportRequestLimitMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=active_settings.session_key,
        session_cookie="__Host-northstar_session" if active_settings.secure_cookies else "northstar_session",
        same_site="lax",
        https_only=active_settings.secure_cookies,
        max_age=3600,
    )
    app.add_middleware(OriginBoundaryMiddleware, settings=active_settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=active_settings)

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["money"] = format_money
    templates.env.filters["percentage"] = format_percentage
    templates.env.filters["quantity"] = format_quantity

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok" if database.health() else "degraded"})

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        dashboard = guidance_service.dashboard()
        household_summary = household_plan_service.summary()
        household_policy = household_policy_repository.settings()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "dashboard": dashboard,
                "summary": dashboard.summary,
                "household_summary": household_summary,
                "household_targets": {
                    item.sleeve: item.target_percentage for item in household_policy.targets
                },
                "household_bitcoin_guardrail": bitcoin_guardrail(
                    household_summary.bitcoin_household_risk_percentage,
                    dashboard.rules.bitcoin_target_percentage,
                    dashboard.rules.bitcoin_caution_percentage,
                    dashboard.rules.bitcoin_review_percentage,
                ),
                "csrf_token": csrf_token(request),
            },
        )

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> HTMLResponse:
        user = request.state.user
        return templates.TemplateResponse(
            request,
            "foundation.html",
            {
                "environment": active_settings.environment,
                "auth_mode": active_settings.auth_mode,
                "user_email": user.email,
                "csrf_token": csrf_token(request),
                "database_healthy": database.health(),
            },
        )

    @app.get("/allocation", response_class=HTMLResponse)
    async def allocation_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "allocation.html",
            {
                "accounts": portfolio.list_accounts(),
                "allocation": None,
                "error_code": None,
                "csrf_token": csrf_token(request),
            },
        )

    @app.get("/plan", response_class=HTMLResponse)
    async def plan_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "plan.html",
            {
                "summary": household_plan_service.summary(),
                "plan_view": None,
                "error_code": None,
                "csrf_token": csrf_token(request),
            },
        )

    @app.post("/plan", dependencies=[Depends(require_csrf)], response_class=HTMLResponse)
    async def calculate_household_plan(
        request: Request,
        available_surplus: Annotated[str, Form()],
        one_time_inflow: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        try:
            plan_view = household_plan_service.plan(
                Decimal(available_surplus),
                Decimal(one_time_inflow) if one_time_inflow.strip() else Decimal("0"),
            )
        except (InvalidOperation, TypeError, ValueError):
            plan_view = None
            error_code = "household_plan_validation_failed"
            status_code = 422
        else:
            error_code = None
            status_code = 200
        return templates.TemplateResponse(
            request,
            "plan.html",
            {
                "summary": plan_view.summary if plan_view else household_plan_service.summary(),
                "plan_view": plan_view,
                "error_code": error_code,
                "csrf_token": csrf_token(request),
            },
            status_code=status_code,
        )

    @app.post("/allocation", dependencies=[Depends(require_csrf)], response_class=HTMLResponse)
    async def calculate_allocation(
        request: Request,
        account_id: Annotated[str, Form()],
        additional_cash: Annotated[str, Form()],
        include_tracked_idle_cash: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        try:
            allocation = guidance_service.allocation_plan(
                account_id,
                additional_cash,
                include_tracked_idle_cash=include_tracked_idle_cash is not None,
            )
        except (InvalidOperation, LookupError, TypeError, ValueError):
            allocation = None
            error_code = "allocation_validation_failed"
            status_code = 422
        else:
            error_code = None
            status_code = 200
        return templates.TemplateResponse(
            request,
            "allocation.html",
            {
                "accounts": portfolio.list_accounts(),
                "allocation": allocation,
                "error_code": error_code,
                "csrf_token": csrf_token(request),
            },
            status_code=status_code,
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        return _settings_response(
            request,
            templates,
            portfolio,
            guidance_repository,
            guidance_service,
            household_policy_repository,
            provider_connections,
            active_settings.schwab_enabled,
            None,
            None,
        )

    @app.post(
        "/integrations/schwab/connect",
        dependencies=[Depends(require_csrf)],
        response_class=HTMLResponse,
    )
    async def connect_schwab(request: Request) -> Response:
        if not active_settings.schwab_enabled or active_schwab_oauth_client is None:
            raise HTTPException(status_code=404, detail="Integration is unavailable")
        try:
            connection, state = provider_connections.start_oauth(oauth_session_binding(request))
        except ProviderConnectionStateError:
            return RedirectResponse(url="/settings?result=schwab_already_connected", status_code=303)
        except OAuthStateError:
            clear_oauth_session(request, rotate_session=True)
            return RedirectResponse(url="/settings?result=schwab_invalid", status_code=303)
        database.add_audit_event(
            "schwab.oauth_started",
            request.state.user.subject,
            subject=connection.id,
        )
        authorization_url = active_schwab_oauth_client.authorization_url(state=state)
        return templates.TemplateResponse(
            request,
            "schwab_confirmation.html",
            {"authorization_url": authorization_url},
            status_code=200,
        )

    @app.get("/integrations/schwab/callback")
    async def schwab_callback(request: Request) -> RedirectResponse:
        if not active_settings.schwab_enabled or active_schwab_oauth_client is None:
            raise HTTPException(status_code=404, detail="Integration is unavailable")
        if len(request.scope.get("query_string", b"")) > MAX_OAUTH_CALLBACK_QUERY_BYTES:
            database.add_audit_event(
                "schwab.oauth_rejected",
                request.state.user.subject,
                metadata={"reason": "callback_too_large"},
            )
            return RedirectResponse(url="/settings?result=schwab_invalid", status_code=303)
        states = request.query_params.getlist("state")
        if len(states) != 1:
            database.add_audit_event(
                "schwab.oauth_rejected",
                request.state.user.subject,
                metadata={"reason": "state_invalid"},
            )
            return RedirectResponse(url="/settings?result=schwab_invalid", status_code=303)
        try:
            attempt = provider_connections.consume_oauth(states[0], oauth_session_binding(request))
        except OAuthStateError:
            database.add_audit_event(
                "schwab.oauth_rejected",
                request.state.user.subject,
                metadata={"reason": "state_invalid"},
            )
            return RedirectResponse(url="/settings?result=schwab_invalid", status_code=303)
        connection = attempt.connection
        clear_oauth_session(request)

        codes = request.query_params.getlist("code")
        errors = request.query_params.getlist("error")
        if errors:
            denied = (
                len(errors) == 1
                and not codes
                and 0 < len(errors[0]) <= 256
                and errors[0].isascii()
                and not any(character.isspace() for character in errors[0])
            )
            reason = "authorization_denied" if denied else "callback_invalid"
            database.add_audit_event(
                "schwab.oauth_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": reason},
            )
            result = "schwab_denied" if denied else "schwab_invalid"
            return RedirectResponse(url=f"/settings?result={result}", status_code=303)
        if len(codes) != 1:
            database.add_audit_event(
                "schwab.oauth_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": "callback_invalid"},
            )
            return RedirectResponse(url="/settings?result=schwab_invalid", status_code=303)
        try:
            grant = await active_schwab_oauth_client.exchange_authorization_code(code=codes[0])
        except (SchwabOAuthError, ValueError) as exc:
            reason = exc.code if isinstance(exc, SchwabOAuthError) else "callback_invalid"
            database.add_audit_event(
                "schwab.oauth_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": reason},
            )
            return RedirectResponse(url="/settings?result=schwab_failed", status_code=303)
        try:
            provider_connections.save_token_grant(attempt, grant)
        except OAuthStateError:
            database.add_audit_event(
                "schwab.oauth_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": "attempt_superseded"},
            )
            return RedirectResponse(url="/settings?result=schwab_invalid", status_code=303)
        except ProviderConnectionStateError:
            database.add_audit_event(
                "schwab.oauth_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": "connection_changed"},
            )
            return RedirectResponse(url="/settings?result=schwab_disconnected", status_code=303)
        clear_oauth_session(request, rotate_session=True)
        database.add_audit_event(
            "schwab.oauth_connected",
            request.state.user.subject,
            subject=connection.id,
        )
        return RedirectResponse(url="/settings?result=schwab_connected", status_code=303)

    @app.post("/integrations/schwab/disconnect", dependencies=[Depends(require_csrf)])
    async def disconnect_schwab(request: Request) -> RedirectResponse:
        if not active_settings.schwab_enabled:
            raise HTTPException(status_code=404, detail="Integration is unavailable")
        connection = provider_connections.for_provider()
        if connection is None or connection.status == "disconnected":
            return RedirectResponse(url="/settings?result=schwab_not_connected", status_code=303)
        provider_connections.disconnect(connection.id)
        if schwab_discovery is not None:
            schwab_discovery.invalidate(connection.id)
        clear_oauth_session(request, rotate_session=True)
        database.add_audit_event(
            "schwab.disconnected",
            request.state.user.subject,
            subject=connection.id,
        )
        return RedirectResponse(url="/settings?result=schwab_disconnected", status_code=303)

    @app.post("/integrations/schwab/discovery", dependencies=[Depends(require_csrf)])
    async def discover_schwab_accounts(request: Request) -> Response:
        connection = provider_connections.for_provider()
        if (
            not active_settings.schwab_enabled
            or connection is None
            or connection.status != "connected"
            or schwab_discovery is None
        ):
            raise HTTPException(status_code=404, detail="Integration is unavailable")

        async def load_accounts() -> tuple:
            try:
                grant = provider_connections.token_grant(connection.id)
            except (ValueError, ProviderConnectionStateError):
                raise SchwabOAuthError("provider_authorization_rejected") from None
            if grant is None or active_schwab_read_client is None:
                raise SchwabOAuthError("provider_authorization_rejected")
            if grant.expires_at <= datetime.now(UTC):
                if active_schwab_oauth_client is None:
                    raise SchwabOAuthError("provider_authorization_rejected")
                refreshed = await active_schwab_oauth_client.refresh_access(refresh_token=grant.refresh_token)
                try:
                    provider_connections.replace_token_grant(connection.id, refreshed)
                except (ValueError, ProviderConnectionStateError):
                    raise SchwabOAuthError("provider_authorization_rejected") from None
                grant = refreshed
            return await active_schwab_read_client.linked_accounts(access_token=grant.access_token)

        try:
            preview = await schwab_discovery.discover(
                connection_id=connection.id, load_accounts=load_accounts
            )
        except DiscoveryError as exc:
            database.add_audit_event(
                "schwab.discovery_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": str(exc)},
            )
            return RedirectResponse(url="/settings?result=schwab_discovery_failed", status_code=303)
        try:
            selected_account_ids = provider_connections.mapped_account_ids(
                connection.id,
                tuple((identity.provider_account_hash, identity.last_four) for identity in preview.accounts),
            )
        except ValueError:
            database.add_audit_event(
                "schwab.discovery_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"reason": "identity_conflict"},
            )
            return RedirectResponse(url="/settings?result=schwab_discovery_failed", status_code=303)
        database.add_audit_event(
            "schwab.discovery_previewed",
            request.state.user.subject,
            subject=connection.id,
            metadata={"count": len(preview.accounts)},
        )
        return templates.TemplateResponse(
            request,
            "schwab_discovery_preview.html",
            {
                "preview": preview,
                "accounts": portfolio.list_accounts(),
                "selected_account_ids": selected_account_ids,
                "csrf_token": csrf_token(request),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/integrations/schwab/discovery/confirm", dependencies=[Depends(require_csrf)])
    async def confirm_schwab_discovery(request: Request) -> RedirectResponse:
        connection = provider_connections.for_provider()
        if connection is None or connection.status != "connected" or schwab_discovery is None:
            raise HTTPException(status_code=404, detail="Integration is unavailable")
        form = await request.form()
        try:
            preview = schwab_discovery.consume(
                token=str(form.get("preview_token", "")), connection_id=connection.id
            )
            choices = tuple(str(form.get(f"mapping_{index}", "")) for index in range(len(preview.accounts)))
            selected = tuple(
                (reference.provider_account_hash, account_id, reference.last_four)
                for reference, account_id in zip(preview.accounts, choices, strict=True)
                if account_id
            )
            provider_connections.map_accounts(connection.id, selected)
        except (DiscoveryError, ValueError, LookupError):
            database.add_audit_event(
                "schwab.mapping_failed",
                request.state.user.subject,
                subject=connection.id,
                metadata={"count": 0},
            )
            return RedirectResponse(url="/settings?result=schwab_mapping_failed", status_code=303)
        database.add_audit_event(
            "schwab.mapping_saved",
            request.state.user.subject,
            subject=connection.id,
            metadata={"count": len(selected)},
        )
        return RedirectResponse(url="/settings?result=schwab_mapping_saved", status_code=303)

    @app.post("/settings/account", dependencies=[Depends(require_csrf)])
    async def save_account_settings(request: Request) -> RedirectResponse:
        form = await request.form()
        account_id = str(form.get("account_id", ""))
        try:
            choices = guidance_repository.security_choices(account_id)
            targets = [
                TargetSetting(
                    choice.id,
                    Decimal(str(form.get(f"target_{choice.id}", ""))),
                    form.get(f"eligible_{choice.id}") is not None,
                )
                for choice in choices
            ]
            guidance_repository.save_account_settings(
                account_id,
                targets,
                fractional_shares=form.get("fractional_shares") is not None,
                dividend_reinvestment=form.get("dividend_reinvestment") is not None,
                actor=request.state.user.subject,
            )
        except (InvalidOperation, LookupError, TypeError, ValueError):
            return RedirectResponse(url="/settings?result=account_invalid", status_code=303)
        return RedirectResponse(url="/settings?result=account_saved", status_code=303)

    @app.post("/settings/portfolio", dependencies=[Depends(require_csrf)])
    async def save_portfolio_settings(request: Request) -> RedirectResponse:
        form = await request.form()
        try:
            guidance_repository.save_rules(
                PortfolioRules(
                    Decimal(str(form.get("bitcoin_target", ""))),
                    Decimal(str(form.get("bitcoin_caution", ""))),
                    Decimal(str(form.get("bitcoin_review", ""))),
                    Decimal(str(form.get("rebalance_threshold", ""))),
                ),
                actor=request.state.user.subject,
            )
        except (InvalidOperation, TypeError, ValueError):
            return RedirectResponse(url="/settings?result=portfolio_invalid", status_code=303)
        return RedirectResponse(url="/settings?result=portfolio_saved", status_code=303)

    @app.post("/settings/envelopes", dependencies=[Depends(require_csrf)])
    async def save_cash_envelopes(request: Request) -> RedirectResponse:
        form = await request.form()
        account_id = str(form.get("account_id", ""))
        names = [str(value) for value in form.getlist("envelope_name")]
        targets = [str(value) for value in form.getlist("envelope_target")]
        assigned = [str(value) for value in form.getlist("envelope_assigned")]
        try:
            if not (len(names) == len(targets) == len(assigned)):
                raise ValueError("Envelope rows are inconsistent")
            envelope_settings: list[EnvelopeSetting] = []
            for name, target, assignment in zip(names, targets, assigned, strict=True):
                if not name.strip() and not target.strip() and not assignment.strip():
                    continue
                if not name.strip():
                    raise ValueError("Envelope name is required")
                envelope_settings.append(
                    EnvelopeSetting(
                        name,
                        Decimal(target) if target.strip() else None,
                        Decimal(assignment) if assignment.strip() else Decimal("0"),
                    )
                )
            actual_cash = sum(
                (
                    item.current_value or Decimal("0")
                    for item in guidance_service.dashboard().summary.holdings
                    if item.fact.account_id == account_id and item.fact.asset_class == "cash"
                ),
                Decimal("0"),
            )
            guidance_repository.save_envelopes(
                account_id,
                envelope_settings,
                actual_cash=actual_cash,
                actor=request.state.user.subject,
            )
        except (InvalidOperation, TypeError, ValueError):
            return RedirectResponse(url="/settings?result=envelopes_invalid", status_code=303)
        return RedirectResponse(url="/settings?result=envelopes_saved", status_code=303)

    @app.post("/settings/household", dependencies=[Depends(require_csrf)])
    async def save_household_policy(request: Request) -> RedirectResponse:
        form = await request.form()
        dashboard = guidance_service.dashboard()
        holdings = dashboard.summary.holdings
        unique_securities = {item.fact.security_id: item.fact for item in holdings}
        try:
            roles = [
                CashRoleSetting(
                    item.fact.account_id,
                    item.fact.security_id,
                    str(form.get(f"cash_role_{item.fact.account_id}_{item.fact.security_id}", "")),
                )
                for item in holdings
                if item.fact.asset_class == "cash"
                and form.get(f"cash_role_{item.fact.account_id}_{item.fact.security_id}")
            ]
            mappings = [
                SecurityPolicyMapping(
                    security_id,
                    str(form.get(f"policy_sleeve_{security_id}", "")),
                    form.get(f"taxable_eligible_{security_id}") is not None,
                )
                for security_id in unique_securities
            ]
            targets = [
                HouseholdTarget(
                    sleeve,
                    Decimal(str(form.get(f"household_target_{sleeve}", ""))),
                )
                for sleeve in HOUSEHOLD_TARGET_SLEEVES
            ]
            lowers = [str(value) for value in form.getlist("bitcoin_band_lower")]
            uppers = [str(value) for value in form.getlist("bitcoin_band_upper")]
            contributions = [str(value) for value in form.getlist("bitcoin_band_contribution")]
            if not (len(lowers) == len(uppers) == len(contributions)):
                raise ValueError("Bitcoin band rows are inconsistent")
            bands = [
                BitcoinContributionBand(Decimal(lower), Decimal(upper), Decimal(contribution))
                for lower, upper, contribution in zip(lowers, uppers, contributions, strict=True)
                if lower.strip() or upper.strip() or contribution.strip()
            ]
            sinking_envelope_id = str(form.get("sinking_fund_envelope_id", "")).strip()
            sinking_contribution = str(form.get("sinking_fund_contribution", "")).strip()
            sinking = (
                SinkingFundSetting(sinking_envelope_id, Decimal(sinking_contribution))
                if sinking_envelope_id or sinking_contribution
                else None
            )
            if sinking is not None and (not sinking_envelope_id or not sinking_contribution):
                raise ValueError("Sinking-fund settings are incomplete")
            household_policy_repository.save_settings(
                cash_roles=roles,
                security_mappings=mappings,
                targets=targets,
                bitcoin_bands=bands,
                sinking_fund=sinking,
                actor=request.state.user.subject,
            )
        except (InvalidOperation, TypeError, ValueError):
            return RedirectResponse(url="/settings?result=household_invalid", status_code=303)
        return RedirectResponse(url="/settings?result=household_saved", status_code=303)

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {
                "accounts": portfolio.list_accounts(),
                "csrf_token": csrf_token(request),
                "error_code": None,
            },
        )

    @app.post("/accounts", dependencies=[Depends(require_csrf)], response_class=HTMLResponse)
    async def create_account(
        request: Request,
        name: Annotated[str, Form()],
        account_type: Annotated[str, Form()],
        tax_treatment: Annotated[str, Form()],
        provider_identifier: Annotated[str, Form()] = "",
        notes: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            portfolio.create_account(
                name=name,
                account_type=account_type,
                tax_treatment=tax_treatment,
                provider_identifier=provider_identifier or None,
                notes=notes or None,
            )
        except ValueError:
            return templates.TemplateResponse(
                request,
                "accounts.html",
                {
                    "accounts": portfolio.list_accounts(),
                    "csrf_token": csrf_token(request),
                    "error_code": "account_validation_failed",
                },
                status_code=422,
            )
        database.add_audit_event(
            "account.created", request.state.user.subject, metadata={"account_type": account_type}
        )
        return RedirectResponse(url="/accounts?created=1", status_code=303)

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request) -> HTMLResponse:
        return _imports_response(request, templates, portfolio, [], None)

    @app.post("/imports/preview", dependencies=[Depends(require_csrf)], response_class=HTMLResponse)
    async def preview_import(
        request: Request,
        adapter_key: Annotated[str, Form()],
        account_id: Annotated[str, Form()],
        upload: Annotated[UploadFile, File()],
    ) -> HTMLResponse:
        payload = await upload.read(MAX_FILE_BYTES + 1)
        await upload.close()
        try:
            preview = import_service.preview(adapter_key, account_id, payload)
        except (ImportValidationError, LookupError) as exc:
            issues = (
                exc.issues
                if isinstance(exc, ImportValidationError)
                else [ValidationIssue(None, None, "account_not_found")]
            )
            return _imports_response(request, templates, portfolio, issues, None, status_code=422)
        if preview.duplicate and preview.batch.status != "preview":
            return _imports_response(request, templates, portfolio, [], "duplicate_import", status_code=200)
        return _preview_response(request, templates, portfolio, preview)

    @app.post("/imports/{batch_id}/commit", dependencies=[Depends(require_csrf)])
    async def commit_import(request: Request, batch_id: str) -> RedirectResponse:
        try:
            import_service.commit(batch_id, request.state.user.subject)
        except (LookupError, ValueError):
            return RedirectResponse(url="/imports?result=commit_failed", status_code=303)
        return RedirectResponse(url="/imports?result=committed", status_code=303)

    @app.post("/imports/{batch_id}/reverse", dependencies=[Depends(require_csrf)])
    async def reverse_import(request: Request, batch_id: str) -> RedirectResponse:
        try:
            import_service.reverse(batch_id, request.state.user.subject)
        except (LookupError, ValueError):
            return RedirectResponse(url="/imports?result=reverse_failed", status_code=303)
        return RedirectResponse(url="/imports?result=reversed", status_code=303)

    @app.post("/demo/verify-storage", dependencies=[Depends(require_csrf)])
    async def verify_storage(request: Request) -> RedirectResponse:
        if active_settings.environment not in {"development", "demo", "test"}:
            return RedirectResponse(url="/", status_code=303)
        database.upsert_secure_record(
            "demo",
            "foundation-check",
            {"message": "Encrypted storage verified", "contains_real_financial_data": False},
        )
        database.add_audit_event("demo.storage_verified", request.state.user.subject)
        return RedirectResponse(url="/?verified=1", status_code=303)

    @app.post("/demo/create-backup", dependencies=[Depends(require_csrf)])
    async def create_backup() -> RedirectResponse:
        if active_settings.environment not in {"development", "demo", "test"}:
            return RedirectResponse(url="/", status_code=303)
        backup_service.create()
        return RedirectResponse(url="/?backup=1", status_code=303)

    return app


def _imports_response(
    request: Request,
    templates: Jinja2Templates,
    portfolio: PortfolioRepository,
    issues: list[ValidationIssue],
    result: str | None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "imports.html",
        {
            "accounts": portfolio.list_accounts(),
            "batches": portfolio.list_import_batches(),
            "issues": issues,
            "result": result or request.query_params.get("result"),
            "csrf_token": csrf_token(request),
        },
        status_code=status_code,
    )


def _preview_response(
    request: Request,
    templates: Jinja2Templates,
    portfolio: PortfolioRepository,
    preview: PreviewSession,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "import_preview.html",
        {
            "account": portfolio.get_account(preview.batch.account_id),
            "preview": preview,
            "csrf_token": csrf_token(request),
        },
    )


def _settings_response(
    request: Request,
    templates: Jinja2Templates,
    portfolio: PortfolioRepository,
    guidance_repository: GuidanceRepository,
    guidance_service: GuidanceService,
    household_policy_repository: HouseholdPolicyRepository,
    provider_connections: ProviderConnectionRepository,
    schwab_enabled: bool,
    result: str | None,
    error_code: str | None,
) -> HTMLResponse:
    accounts = portfolio.list_accounts()
    target_forms = []
    for account in accounts:
        choices = guidance_repository.security_choices(account.id)
        current_targets = {target.security_id: target for target in guidance_repository.targets(account.id)}
        target_forms.append(
            {
                "account": account,
                "choices": choices,
                "targets": current_targets,
                "preference": guidance_repository.account_preference(account.id),
            }
        )
    dashboard = guidance_service.dashboard()
    household_policy = household_policy_repository.settings()
    try:
        requested_band_rows = int(request.query_params.get("band_rows", "5"))
    except ValueError:
        requested_band_rows = 5
    bitcoin_band_row_count = min(
        MAX_BITCOIN_BANDS,
        max(1, requested_band_rows, len(household_policy.bitcoin_bands)),
    )
    cash_accounts = []
    all_envelopes = guidance_repository.envelope_facts()
    for account in accounts:
        if account.account_type != "cash":
            continue
        actual_cash = sum(
            (
                item.current_value or Decimal("0")
                for item in dashboard.summary.holdings
                if item.fact.account_id == account.id and item.fact.asset_class == "cash"
            ),
            Decimal("0"),
        )
        cash_accounts.append(
            {
                "account": account,
                "actual_cash": actual_cash,
                "envelopes": [item for item in all_envelopes if item.account_id == account.id],
            }
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "target_forms": target_forms,
            "cash_accounts": cash_accounts,
            "rules": guidance_repository.rules(),
            "household_policy": household_policy,
            "bitcoin_band_blank_rows": (bitcoin_band_row_count - len(household_policy.bitcoin_bands)),
            "bitcoin_band_row_count": bitcoin_band_row_count,
            "max_bitcoin_bands": MAX_BITCOIN_BANDS,
            "household_mappings": {item.security_id: item for item in household_policy.security_mappings},
            "household_targets": {item.sleeve: item for item in household_policy.targets},
            "household_target_sleeves": HOUSEHOLD_TARGET_SLEEVES,
            "policy_sleeves": HOUSEHOLD_POLICY_SLEEVES,
            "policy_holdings": dashboard.summary.holdings,
            "policy_securities": {
                item.fact.security_id: item for item in dashboard.summary.holdings
            }.values(),
            "cash_role_lookup": {
                (item.account_id, item.security_id): item.role for item in household_policy.cash_roles
            },
            "sinking_envelopes": [item for item in all_envelopes if item.target_amount is not None],
            "schwab_enabled": schwab_enabled,
            "schwab_connection": provider_connections.for_provider(),
            "result": result or request.query_params.get("result"),
            "error_code": error_code,
            "csrf_token": csrf_token(request),
        },
    )


app = create_app()

from __future__ import annotations

from app.main_shared import *
from app.timo_guild_identity import (
    externalize_timo_guild_names,
    require_timo_guild_identity,
    resolve_timo_guild_identity,
    timo_guild_contract_fields,
    timo_guild_display_name,
    timo_guild_storage_name,
)
from app.timo_incremental_materialization import (
    TimoDbSyncLease,
    TimoCircuitOpen,
    TimoIncrementalSyncError,
    TimoSyncLockBusy,
    check_timo_circuit_breaker,
    materialize_timo_revenue_snapshot,
    record_timo_circuit_failure,
    record_timo_circuit_success,
    record_timo_sync_attempt_failure,
    schedule_timo_sync_retry,
    timo_external_feed_status,
)
from app.timo_bi_mart import (
    TimoBiMartError,
    TimoBiMartQueryTimeout,
    query_timo_bi_mart,
)


class TimoServiceMixin:
    def _timo_yesterday_export_date_bj(self) -> str:
        return (datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date() - timedelta(days=1)).isoformat()

    def export_timo_yesterday_ids_xlsx(self, *, guild_name: str, user: Optional[Dict[str, Any]], date_bj: Optional[str] = None) -> bytes:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name_required')
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        target_date = str(date_bj or self._timo_yesterday_export_date_bj()).strip()
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', target_date):
            raise HTTPException(status_code=400, detail='invalid_date')
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, timo_id
                FROM ops_timo_intake_items
                WHERE guild_name = ?
                  AND date(created_at, '+8 hours') = ?
                  AND COALESCE(timo_id, '') != ''
                ORDER BY timo_id ASC, created_at ASC, item_id ASC
                """,
                (normalized_guild, target_date),
            ).fetchall()
        seen_ids: set[str] = set()
        export_rows: List[Tuple[str, str]] = []
        for row in rows:
            timo_id = self._normalize_timo_id(row['timo_id'])
            if not re.fullmatch(r'\d{12}', timo_id):
                continue
            if timo_id in seen_ids:
                continue
            seen_ids.add(timo_id)
            export_rows.append((target_date, timo_id))

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '昨日ID表格'
        sheet.append(['日期', 'id'])
        header_fill = PatternFill(fill_type='solid', fgColor='DBEAFE')
        header_font = Font(bold=True, color='1E3A8A')
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
        for row in export_rows:
            sheet.append(list(row))
        sheet.freeze_panes = 'A2'
        sheet.column_dimensions['A'].width = 14
        sheet.column_dimensions['B'].width = 18
        for row_index in range(2, sheet.max_row + 1):
            sheet.cell(row=row_index, column=1).alignment = Alignment(horizontal='center')
            sheet.cell(row=row_index, column=2).alignment = Alignment(horizontal='left')
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def timo_yesterday_ids_export_filename(self, *, guild_name: str, date_bj: Optional[str] = None) -> str:
        display_guild = self._timo_intake_guild_display_name(str(guild_name or '').strip())
        safe_guild = re.sub(r'[^A-Za-z0-9]+', '', display_guild) or 'Timo'
        target_date = str(date_bj or self._timo_yesterday_export_date_bj()).strip()
        try:
            compact_date = datetime.strptime(target_date, '%Y-%m-%d').strftime('%y%m%d')
        except Exception:
            compact_date = re.sub(r'\D+', '', target_date)[-6:] or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%y%m%d')
        return f'{safe_guild}{compact_date}.xlsx'

    @staticmethod
    def _timo_export_anchor_id(value: Any) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        if ':' in text:
            text = text.split(':', 1)[1].strip()
        return text

    @staticmethod
    def _timo_export_numeric_id(value: Any) -> str:
        text = str(value or '').strip()
        if ':' in text:
            text = text.split(':', 1)[1].strip()
        digits = re.sub(r'\D+', '', text)
        return digits if digits else text

    def _timo_resolve_export_executor(self, *, guild_name: str, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name_required')
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        executor = self.resolve_timo_guild_executor(normalized_guild)
        if not executor or not executor.get('enabled'):
            raise HTTPException(status_code=404, detail='timo_guild_executor_not_found')
        if not str(executor.get('platform_authorization') or '').strip():
            raise HTTPException(status_code=400, detail='timo_ticket_not_configured')
        return executor

    def _refresh_timo_export_anchor_cache(self, *, executor: Dict[str, Any]) -> Dict[str, Any]:
        today_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()
        result = self._fetch_timo_guild_anchor_daily_count(executor=executor, stat_date=today_bj)
        if str(result.get('status') or '').lower() != 'success':
            raise HTTPException(status_code=502, detail=f'timo_anchor_full_scan_failed:{str(result.get("error") or "")[:160]}')
        return result

    def _timo_seen_anchor_rows(self, *, executor: Dict[str, Any]) -> List[Dict[str, Any]]:
        executor_key = self._guild_anchor_executor_key(executor)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT anchor_id, anchor_name, created_at, created_date_bj, is_real_person, guild_name, last_seen_at
                FROM guild_anchor_seen
                WHERE guild_executor_key = ?
                ORDER BY created_at ASC, anchor_id ASC
                """,
                (executor_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _timo_exported_id_set(self, *, executor: Dict[str, Any], export_kind: str) -> set[str]:
        executor_key = self._guild_anchor_executor_key(executor)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT timo_id
                FROM timo_anchor_exported_ids
                WHERE guild_executor_key = ? AND export_kind = ?
                """,
                (executor_key, str(export_kind or '').strip()),
            ).fetchall()
        return {str(row['timo_id'] or '').strip() for row in rows if str(row['timo_id'] or '').strip()}

    def _record_timo_exported_ids(
        self,
        *,
        executor: Dict[str, Any],
        export_kind: str,
        rows: List[Dict[str, Any]],
    ) -> None:
        normalized_kind = str(export_kind or '').strip()
        if not normalized_kind or not rows:
            return
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        now_iso = utc_now()
        values = []
        for row in rows:
            timo_id = str(row.get('timo_id') or '').strip()
            if not timo_id:
                continue
            payload = {
                key: value
                for key, value in dict(row).items()
                if key not in {'timo_id'} and value not in (None, '')
            }
            values.append((
                executor_key,
                guild_name,
                normalized_kind,
                timo_id,
                now_iso,
                str(row.get('last_seen_at') or now_iso),
                float(row.get('source_value') or row.get('total_income') or 0.0),
                json.dumps(payload, ensure_ascii=False, default=str),
            ))
        if not values:
            return
        with self.db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO timo_anchor_exported_ids (
                    guild_executor_key, guild_name, export_kind, timo_id,
                    first_exported_at, last_seen_at, source_value, source_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_executor_key, export_kind, timo_id) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    last_seen_at = excluded.last_seen_at,
                    source_value = excluded.source_value,
                    source_payload = excluded.source_payload
                """,
                values,
            )
            conn.commit()

    def _record_timo_anchor_export_cache_rows(
        self,
        *,
        executor: Dict[str, Any],
        export_kind: str,
        data_date_bj: str,
        period_type: str,
        rows: List[Dict[str, Any]],
    ) -> int:
        normalized_kind = str(export_kind or '').strip()
        normalized_date = str(data_date_bj or '').strip()
        if not normalized_kind or not normalized_date:
            return 0
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        now_iso = utc_now()
        values = []
        for row in rows:
            timo_id = str(row.get('timo_id') or '').strip()
            if not timo_id:
                continue
            values.append((
                executor_key,
                guild_name,
                normalized_kind,
                normalized_date,
                str(period_type or 'day').strip() or 'day',
                timo_id,
                str(row.get('anchor_name') or row.get('nick_name') or '').strip(),
                float(row.get('diamond_amount') or row.get('total_income') or 0.0),
                json.dumps({k: v for k, v in dict(row).items() if k not in {'timo_id'}}, ensure_ascii=False, default=str),
                now_iso,
                now_iso,
            ))
        if not values:
            return 0
        with self.db.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO timo_anchor_export_cache (
                    guild_executor_key, guild_name, export_kind, data_date_bj, period_type,
                    timo_id, anchor_name, diamond_amount, source_payload, first_cached_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_executor_key, export_kind, timo_id) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    data_date_bj = excluded.data_date_bj,
                    period_type = excluded.period_type,
                    anchor_name = CASE WHEN excluded.anchor_name != '' THEN excluded.anchor_name ELSE timo_anchor_export_cache.anchor_name END,
                    diamond_amount = excluded.diamond_amount,
                    source_payload = excluded.source_payload,
                    updated_at = excluded.updated_at
                """,
                values,
            )
            conn.commit()
            return max(0, conn.total_changes - before)

    def _timo_cached_anchor_export_rows(
        self,
        *,
        executor: Dict[str, Any],
        export_kind: str,
        data_date_bj: str,
    ) -> List[Dict[str, Any]]:
        executor_key = self._guild_anchor_executor_key(executor)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT timo_id, anchor_name, diamond_amount, data_date_bj, first_cached_at, updated_at
                FROM timo_anchor_export_cache
                WHERE guild_executor_key = ?
                  AND export_kind = ?
                  AND data_date_bj = ?
                ORDER BY diamond_amount DESC, timo_id ASC
                """,
                (executor_key, str(export_kind or '').strip(), str(data_date_bj or '').strip()),
            ).fetchall()
        return [dict(row) for row in rows]

    def _timo_cached_anchor_export_ids_before_date(
        self,
        *,
        executor: Dict[str, Any],
        export_kind: str,
        data_date_bj: str,
    ) -> set[str]:
        executor_key = self._guild_anchor_executor_key(executor)
        normalized_kind = str(export_kind or '').strip()
        normalized_date = str(data_date_bj or '').strip()
        if not normalized_kind or not normalized_date:
            return set()
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT timo_id
                FROM timo_anchor_export_cache
                WHERE guild_executor_key = ?
                  AND export_kind = ?
                  AND data_date_bj < ?
                """,
                (executor_key, normalized_kind, normalized_date),
            ).fetchall()
        return {str(row['timo_id'] or '').strip() for row in rows if str(row['timo_id'] or '').strip()}

    def materialize_timo_real_person_ids_cache(
        self,
        *,
        guild_name: str,
        user: Optional[Dict[str, Any]],
        as_of_date_bj: Optional[str] = None,
        refresh_anchor_cache: bool = True,
    ) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        target_date = self._timo_resolve_latest_complete_date_bj(as_of_date_bj)
        executor = self._timo_resolve_export_executor(guild_name=normalized_guild, user=user)
        scan_result: Dict[str, Any] = {}
        if refresh_anchor_cache:
            scan_result = self._refresh_timo_export_anchor_cache(executor=executor)
        rows = self._timo_seen_anchor_rows(executor=executor)
        cache_rows: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in rows:
            if int(row.get('is_real_person') or 0) != 1:
                continue
            created_date = str(row.get('created_date_bj') or '').strip()
            if created_date and created_date > target_date.isoformat():
                continue
            timo_id = self._timo_export_numeric_id(row.get('anchor_id'))
            if not timo_id or timo_id in seen_ids:
                continue
            seen_ids.add(timo_id)
            cache_rows.append({
                'timo_id': timo_id,
                'anchor_name': str(row.get('anchor_name') or '').strip(),
                'created_date_bj': created_date,
                'created_at': int(row.get('created_at') or 0),
                'last_seen_at': row.get('last_seen_at') or '',
            })
        baseline_ids = self._timo_cached_anchor_export_ids_before_date(
            executor=executor,
            export_kind='real_person',
            data_date_bj=target_date.isoformat(),
        )
        new_cache_rows = [row for row in cache_rows if str(row.get('timo_id') or '').strip() not in baseline_ids]
        changed = self._record_timo_anchor_export_cache_rows(
            executor=executor,
            export_kind='real_person',
            data_date_bj=target_date.isoformat(),
            period_type='day',
            rows=new_cache_rows,
        )
        return {
            'ok': True,
            'guild_name': normalized_guild,
            'export_kind': 'real_person',
            'data_date_bj': target_date.isoformat(),
            'candidate_count': len(new_cache_rows),
            'total_candidate_count': len(cache_rows),
            'baseline_count': len(baseline_ids),
            'cached_change_count': changed,
            'scan': scan_result,
        }

    def materialize_timo_first_20k_diamonds_cache(
        self,
        *,
        guild_name: str,
        user: Optional[Dict[str, Any]],
        as_of_date_bj: Optional[str] = None,
        refresh_anchor_cache: bool = True,
    ) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        target_date = self._timo_resolve_latest_complete_date_bj(as_of_date_bj)
        executor = self._timo_resolve_export_executor(guild_name=normalized_guild, user=user)
        scan_result: Dict[str, Any] = {}
        if refresh_anchor_cache:
            scan_result = self._refresh_timo_export_anchor_cache(executor=executor)
        revenue_content, revenue_filename = self.export_timo_guild_revenue_xlsx(
            guild_name=normalized_guild,
            user=user,
            export_type='day',
            date_bj=target_date.isoformat(),
        )
        revenue_rows = self._parse_timo_revenue_rows(revenue_content)
        seen_rows = self._timo_seen_anchor_rows(executor=executor)
        name_by_id = {
            self._timo_export_numeric_id(row.get('anchor_id')): str(row.get('anchor_name') or '').strip()
            for row in seen_rows
        }
        income_by_id: Dict[str, Dict[str, Any]] = {}
        for row in revenue_rows:
            timo_id = str(row.get('timo_id') or '').strip()
            if not timo_id:
                continue
            current = income_by_id.setdefault(timo_id, {**row, 'total_income': 0.0})
            current['total_income'] = float(current.get('total_income') or 0.0) + float(row.get('total_income') or 0.0)
            for key in ('nick_name', 'user_uuid', 'host_role', 'quality_host'):
                if not current.get(key) and row.get(key):
                    current[key] = row.get(key)
        qualified_rows = []
        for timo_id, row in income_by_id.items():
            total_income = float(row.get('total_income') or 0.0)
            if total_income < 20000:
                continue
            qualified_rows.append({
                'timo_id': timo_id,
                'anchor_name': str(row.get('nick_name') or name_by_id.get(timo_id) or '').strip(),
                'diamond_amount': total_income,
                'revenue_filename': revenue_filename,
            })
        qualified_rows.sort(key=lambda item: (-float(item.get('diamond_amount') or 0.0), str(item.get('timo_id') or '')))
        baseline_ids = self._timo_cached_anchor_export_ids_before_date(
            executor=executor,
            export_kind='first_20k_diamonds',
            data_date_bj=target_date.isoformat(),
        )
        new_qualified_rows = [
            row for row in qualified_rows
            if str(row.get('timo_id') or '').strip() not in baseline_ids
        ]
        changed = self._record_timo_anchor_export_cache_rows(
            executor=executor,
            export_kind='first_20k_diamonds',
            data_date_bj=target_date.isoformat(),
            period_type='day',
            rows=new_qualified_rows,
        )
        return {
            'ok': True,
            'guild_name': normalized_guild,
            'export_kind': 'first_20k_diamonds',
            'data_date_bj': target_date.isoformat(),
            'candidate_count': len(new_qualified_rows),
            'total_candidate_count': len(qualified_rows),
            'baseline_count': len(baseline_ids),
            'cached_change_count': changed,
            'scan': scan_result,
            'revenue_filename': revenue_filename,
        }

    def materialize_timo_anchor_export_cache(
        self,
        *,
        guild_name: str,
        user: Optional[Dict[str, Any]],
        kind: str = 'all',
        as_of_date_bj: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_kind = str(kind or 'all').strip().lower()
        results: Dict[str, Any] = {}
        if normalized_kind in {'all', 'daily', 'revenue_yesterday'}:
            results['revenue_yesterday'] = self.materialize_timo_revenue_export_cache(
                guild_name=guild_name,
                user=user,
                period='yesterday',
            )
        if normalized_kind in {'all', 'daily', 'real_person'}:
            results['real_person'] = self.materialize_timo_real_person_ids_cache(
                guild_name=guild_name,
                user=user,
                as_of_date_bj=as_of_date_bj,
                refresh_anchor_cache=True,
            )
        if normalized_kind in {'all', 'weekly', 'revenue_last_week'}:
            results['revenue_last_week'] = self.materialize_timo_revenue_export_cache(
                guild_name=guild_name,
                user=user,
                period='last_week',
            )
        if normalized_kind in {'all', 'daily', 'weekly', 'first_20k_diamonds', 'first20k'}:
            results['first_20k_diamonds'] = self.materialize_timo_first_20k_diamonds_cache(
                guild_name=guild_name,
                user=user,
                as_of_date_bj=as_of_date_bj,
                refresh_anchor_cache=('real_person' not in results),
            )
        return {'ok': True, 'guild_name': str(guild_name or '').strip(), 'kind': normalized_kind, 'results': results}

    @staticmethod
    def _timo_export_style_sheet(sheet: Any) -> None:
        header_fill = PatternFill(fill_type='solid', fgColor='DBEAFE')
        header_font = Font(bold=True, color='1E3A8A')
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
        sheet.freeze_panes = 'A2'
        for column_cells in sheet.columns:
            max_length = 10
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                value = '' if cell.value is None else str(cell.value)
                max_length = max(max_length, min(len(value) + 2, 42))
            sheet.column_dimensions[column_letter].width = max_length

    def export_timo_real_person_ids_xlsx(self, *, guild_name: str, user: Optional[Dict[str, Any]], as_of_date_bj: Optional[str] = None) -> Tuple[bytes, str]:
        normalized_guild = str(guild_name or '').strip()
        target_date = self._timo_resolve_latest_complete_date_bj(as_of_date_bj)
        executor = self._timo_resolve_export_executor(guild_name=normalized_guild, user=user)
        rows = self._timo_cached_anchor_export_rows(executor=executor, export_kind='real_person', data_date_bj=target_date.isoformat())
        if not rows and not ((self._timo_export_cache_status_for_executor(executor).get('real_person') or {}).get('ready')):
            raise HTTPException(status_code=404, detail=f'timo_anchor_export_cache_not_ready:real_person:{target_date.isoformat()}')
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '真人id表'
        sheet.append(['主播id', '主播名'])
        for row in rows:
            sheet.append([
                str(row.get('timo_id') or '').strip(),
                str(row.get('anchor_name') or '').strip(),
            ])
        self._timo_export_style_sheet(sheet)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue(), self.timo_real_person_ids_export_filename(guild_name=normalized_guild, date_bj=target_date.isoformat())

    def timo_real_person_ids_export_filename(self, *, guild_name: str, date_bj: Optional[str] = None) -> str:
        display_guild = self._timo_intake_guild_display_name(str(guild_name or '').strip())
        safe_guild = re.sub(r'[^A-Za-z0-9]+', '', display_guild) or 'Timo'
        raw_date = str(date_bj or '').strip()
        compact_date = re.sub(r'\D+', '', raw_date)[-6:] if raw_date else ''
        compact_date = compact_date or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%y%m%d')
        return f'{safe_guild}RealPersonIds{compact_date}.xlsx'

    @staticmethod
    def _timo_revenue_header_map(sheet: Any) -> Dict[str, int]:
        header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
        return {str(value or '').strip().lower(): index for index, value in enumerate(header_row, start=1) if str(value or '').strip()}

    @staticmethod
    def _timo_revenue_col(header_map: Dict[str, int], *names: str) -> Optional[int]:
        for name in names:
            found = header_map.get(str(name or '').strip().lower())
            if found:
                return found
        return None

    def _timo_join_times_by_executor(self, executor_key: str) -> Dict[str, str]:
        normalized_key = str(executor_key or '').strip()
        if not normalized_key:
            return {}
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT timo_id, COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj) AS joined_guild_at_bj
                FROM timo_external_streamers
                WHERE guild_executor_key=?
                  AND COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj)<>''
                """,
                (normalized_key,),
            ).fetchall()
        return {
            str(row['timo_id'] or '').strip(): str(row['joined_guild_at_bj'] or '').strip()
            for row in rows
            if str(row['timo_id'] or '').strip() and str(row['joined_guild_at_bj'] or '').strip()
        }

    def _fetch_timo_join_times_for_executor(self, executor: Dict[str, Any]) -> Dict[str, str]:
        page_size = self.guild_anchor_daily_stats_page_size
        max_pages = self.guild_anchor_daily_stats_max_pages
        join_times: Dict[str, str] = {}
        scanned = 0
        total: Optional[int] = None
        pagination_complete = False
        for page in range(1, max_pages + 1):
            payload = self._fetch_timo_guild_host_page(
                executor=executor,
                page=page,
                page_size=page_size,
                timeout_seconds=float(executor.get('request_timeout_seconds') or 30),
            )
            items = payload.get('items') if isinstance(payload.get('items'), list) else []
            if total is None and payload.get('total_anchors') is not None:
                total = int(payload.get('total_anchors') or 0)
            if not items:
                pagination_complete = True
                break
            for item in items:
                timo_id = self._timo_export_numeric_id(
                    item.get('userId') or item.get('user_id') or item.get('timoId') or item.get('sid')
                )
                join_time = self._timo_epoch_ms_to_bj_text(item.get('joinTime') or item.get('created_at'))
                if not timo_id or not join_time:
                    raise HTTPException(status_code=502, detail='timo_join_time_snapshot_invalid')
                join_times[timo_id] = join_time
            scanned += len(items)
            if total is not None and scanned >= total:
                pagination_complete = scanned == total
                break
            if len(items) < page_size:
                pagination_complete = True
                break
        if not pagination_complete or not join_times or (total is not None and scanned != total):
            raise HTTPException(
                status_code=502,
                detail=f'timo_join_time_pagination_incomplete:expected={total}:scanned={scanned}',
            )
        return join_times

    def _fetch_timo_complete_streamer_snapshot(
        self,
        executor: Dict[str, Any],
        *,
        max_attempts: int = 2,
    ) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, str]]:
        last_error = 'timo_streamer_snapshot_invalid'
        for _attempt in range(max(1, int(max_attempts or 1))):
            page_size = self.guild_anchor_daily_stats_page_size
            max_pages = self.guild_anchor_daily_stats_max_pages
            streamers: List[Dict[str, Any]] = []
            scanned = 0
            total = None
            pagination_complete = False
            for page in range(1, max_pages + 1):
                payload = self._fetch_timo_guild_host_page(
                    executor=executor,
                    page=page,
                    page_size=page_size,
                    timeout_seconds=float(executor.get('request_timeout_seconds') or 30),
                )
                items = payload.get('items') if isinstance(payload.get('items'), list) else []
                if total is None:
                    total = payload.get('total_anchors')
                if not items:
                    pagination_complete = True
                    break
                streamers.extend(items)
                scanned += len(items)
                if total is not None and scanned >= int(total or 0):
                    pagination_complete = scanned == int(total or 0)
                    break
                if len(items) < page_size:
                    pagination_complete = True
                    break
            expected_count = int(total) if total is not None else scanned
            if not pagination_complete or scanned != expected_count:
                last_error = f'timo_streamer_pagination_incomplete:expected={expected_count}:scanned={scanned}'
                continue
            streamer_ids = [
                self._timo_export_numeric_id(
                    item.get('userId') or item.get('user_id') or item.get('timoId') or item.get('sid')
                )
                for item in streamers
            ]
            if any(not timo_id for timo_id in streamer_ids):
                last_error = 'timo_streamer_id_missing'
                continue
            if len(set(streamer_ids)) != len(streamer_ids):
                last_error = 'timo_streamer_id_duplicate'
                continue
            join_times = {
                timo_id: self._timo_epoch_ms_to_bj_text(item.get('joinTime') or item.get('created_at'))
                for item, timo_id in zip(streamers, streamer_ids)
            }
            if any(not value for value in join_times.values()):
                last_error = 'timo_join_time_missing'
                continue
            return streamers, streamer_ids, join_times
        guild_name = str(executor.get('guild_name') or '').strip()
        raise RuntimeError(f'{last_error}:{guild_name}')

    def _apply_timo_join_time_contract(
        self,
        detail_sheet: Any,
        *,
        join_time_by_timo_id: Dict[str, str],
    ) -> int:
        header_map = self._timo_revenue_header_map(detail_sheet)
        id_col = self._timo_revenue_col(header_map, 'userId', 'user id', '用戶id', '用户id')
        registered_col = self._timo_revenue_col(
            header_map, '入会时间', '入會時間', '主播註冊時間', '主播注册时间', 'registration time'
        )
        if not registered_col:
            return 0
        if not id_col or not join_time_by_timo_id:
            raise HTTPException(status_code=502, detail='timo_join_time_snapshot_missing')
        missing_ids: List[str] = []
        updated = 0
        for row_index in range(2, detail_sheet.max_row + 1):
            timo_id = self._timo_export_numeric_id(detail_sheet.cell(row=row_index, column=id_col).value)
            if not timo_id:
                continue
            join_time = str(join_time_by_timo_id.get(timo_id) or '').strip()
            if not join_time:
                missing_ids.append(timo_id)
                continue
            detail_sheet.cell(row=row_index, column=registered_col).value = join_time
            updated += 1
        if missing_ids:
            sample = ','.join(missing_ids[:3])
            raise HTTPException(
                status_code=502,
                detail=f'timo_join_time_missing_for_revenue_rows:count={len(missing_ids)}:sample={sample}',
            )
        detail_sheet.cell(row=1, column=registered_col).value = '入会时间'
        return updated

    def _timo_account_registration_times_from_revenue_content(self, content: bytes) -> Dict[str, str]:
        try:
            workbook = load_workbook(io.BytesIO(content), data_only=True)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'timo_revenue_export_invalid_xlsx:{str(exc)[:120]}') from exc
        detail_sheet = next((workbook[name] for name in workbook.sheetnames if name != '收益统计'), None)
        if detail_sheet is None:
            return {}
        header_map = self._timo_revenue_header_map(detail_sheet)
        id_col = self._timo_revenue_col(header_map, 'userId', 'user id', '用戶id', '用户id')
        registered_col = self._timo_revenue_col(
            header_map, '主播註冊時間', '主播注册时间', 'registration time'
        )
        if not id_col or not registered_col:
            return {}
        result: Dict[str, str] = {}
        for row_index in range(2, detail_sheet.max_row + 1):
            timo_id = self._timo_export_numeric_id(detail_sheet.cell(row=row_index, column=id_col).value)
            registered_at = str(detail_sheet.cell(row=row_index, column=registered_col).value or '').strip()
            if timo_id and registered_at:
                result[timo_id] = registered_at
        return result

    def _store_timo_account_registration_times(
        self,
        *,
        executor_key: str,
        registration_time_by_timo_id: Dict[str, str],
    ) -> int:
        if not executor_key or not registration_time_by_timo_id:
            return 0
        updated = 0
        with self.db.connect() as conn:
            for timo_id, registered_at in registration_time_by_timo_id.items():
                cursor = conn.execute(
                    """
                    UPDATE timo_external_streamers
                    SET timo_registered_at_bj=?, updated_at=?
                    WHERE guild_executor_key=? AND timo_id=?
                    """,
                    (registered_at, utc_now(), executor_key, timo_id),
                )
                updated += max(0, int(cursor.rowcount or 0))
            conn.commit()
        return updated

    def _parse_timo_revenue_rows(self, content: bytes) -> List[Dict[str, Any]]:
        return self._parse_timo_revenue_detail_rows(content)

    def _parse_timo_revenue_detail_rows(self, content: bytes) -> List[Dict[str, Any]]:
        try:
            workbook = load_workbook(io.BytesIO(content), data_only=True)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'timo_revenue_export_invalid_xlsx:{str(exc)[:120]}') from exc
        detail_sheet = None
        for sheet_name in workbook.sheetnames:
            if sheet_name != '收益统计':
                detail_sheet = workbook[sheet_name]
                break
        if detail_sheet is None:
            raise HTTPException(status_code=502, detail='timo_revenue_detail_sheet_missing')
        header_map = self._timo_revenue_header_map(detail_sheet)
        id_col = self._timo_revenue_col(header_map, 'userId', 'user id', '用戶id', '用户id')
        income_col = self._timo_revenue_col(header_map, '1v1 total income', '1v1總收益', '1v1总收益')
        if not id_col or not income_col:
            raise HTTPException(status_code=502, detail='timo_revenue_required_columns_missing')
        nick_col = self._timo_revenue_col(header_map, 'nickName', '主播暱稱', '主播昵称')
        uuid_col = self._timo_revenue_col(header_map, 'userUuid', '用戶uuid', '用户uuid')
        role_col = self._timo_revenue_col(header_map, 'hostRole', '主播身份', 'Host Role')
        quality_col = self._timo_revenue_col(header_map, 'Quality Host', '優質主播', '优质主播')
        qualified_col = self._timo_revenue_col(header_map, '1v1 host qualified revenue this week', '本週1v1主播達標收益', '本周1v1主播达标收益')
        matching_col = self._timo_revenue_col(header_map, 'Matching call earnings', '匹配通話收益', '匹配通话收益')
        private_message_col = self._timo_revenue_col(header_map, 'Private message earnings', '私信消息收益')
        private_gift_col = self._timo_revenue_col(header_map, 'Private gift earnings', '私信禮物收益', '私信礼物收益')
        call_income_col = self._timo_revenue_col(header_map, '1v1 call earnings', '1v1通話收益', '1v1通话收益')
        online_hours_col = self._timo_revenue_col(header_map, '在線時長(單位：h）', '在線時長(單位：h)', '在线时长(单位：h)', 'online duration(h)', 'online hours')
        call_count_col = self._timo_revenue_col(header_map, '通話數', '通话数', 'Call Count')
        quality_revenue_col = self._timo_revenue_col(header_map, 'Specific Revenue for Quality Host', '優質主播特定場景收益', '优质主播特定场景收益')
        registered_col = self._timo_revenue_col(
            header_map, '入会时间', '入會時間', '主播註冊時間', '主播注册时间', 'registration time'
        )
        parsed_rows: List[Dict[str, Any]] = []
        for row_index in range(2, detail_sheet.max_row + 1):
            timo_id = self._timo_export_numeric_id(detail_sheet.cell(row=row_index, column=id_col).value)
            if not timo_id:
                continue
            quality_value = detail_sheet.cell(row=row_index, column=quality_col).value if quality_col else ''
            parsed_rows.append({
                'timo_id': timo_id,
                'nick_name': detail_sheet.cell(row=row_index, column=nick_col).value if nick_col else '',
                'user_uuid': detail_sheet.cell(row=row_index, column=uuid_col).value if uuid_col else '',
                'host_role': detail_sheet.cell(row=row_index, column=role_col).value if role_col else '',
                'joined_guild_at_bj': detail_sheet.cell(row=row_index, column=registered_col).value if registered_col else '',
                'quality_host': quality_value,
                'total_income': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=income_col).value),
                'qualified_revenue': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=qualified_col).value) if qualified_col else 0.0,
                'matching_income': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=matching_col).value) if matching_col else 0.0,
                'private_message_income': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=private_message_col).value) if private_message_col else 0.0,
                'private_gift_income': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=private_gift_col).value) if private_gift_col else 0.0,
                'call_income': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=call_income_col).value) if call_income_col else 0.0,
                'online_hours': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=online_hours_col).value) if online_hours_col else 0.0,
                'call_count': int(self._timo_revenue_number(detail_sheet.cell(row=row_index, column=call_count_col).value)) if call_count_col else 0,
                'quality_revenue': self._timo_revenue_number(detail_sheet.cell(row=row_index, column=quality_revenue_col).value) if quality_revenue_col else 0.0,
            })
        return parsed_rows

    @staticmethod
    def _timo_bool_flag(value: Any) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        text = str(value or '').strip().lower()
        return 1 if text in {'1', 'true', 'yes', 'y', '是', 'yes', '優質', '优质', 'verified'} else 0

    @staticmethod
    def _timo_epoch_ms_to_bj_text(value: Any) -> str:
        # Timo CMS timestamps are already displayed in UTC+8.
        # Never reinterpret it in the guild country's local timezone.
        if value in (None, ''):
            return ''
        try:
            numeric = int(float(str(value).strip()))
            if numeric > 10_000_000_000:
                numeric = numeric // 1000
            if numeric <= 0:
                return ''
            return datetime.fromtimestamp(numeric, tz=timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return str(value or '').strip()

    @staticmethod
    def _timo_task_reward_diamonds(task: Dict[str, Any]) -> float:
        raw = task.get('reward')
        if not isinstance(raw, list):
            return 0.0
        total = 0.0
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                total += float(str(item.get('count') or '0').replace(',', ''))
            except Exception:
                continue
        return total

    def _fetch_timo_revenue_export_content_unchecked(
        self,
        *,
        executor: Dict[str, Any],
        date_from_bj: str,
        date_to_bj: str,
        time_type: Any = '',
    ) -> bytes:
        proxy_url = self._resolve_executor_proxy_url(executor)
        proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
        export_url = self._timo_guild_revenue_export_url(
            executor=executor,
            date_from_bj=date_from_bj,
            date_to_bj=date_to_bj,
            time_type=time_type,
        )
        try:
            response = requests.get(export_url, stream=True, proxies=proxies, timeout=90)
            response.raise_for_status()
            content = response.content
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'timo_revenue_export_download_failed:{str(exc)[:160]}') from exc
        if not content:
            raise HTTPException(status_code=502, detail='timo_revenue_export_empty_file')
        return content

    def _fetch_timo_guild_task_rows(self, *, executor: Dict[str, Any]) -> List[Dict[str, Any]]:
        body = self._timo_guild_api_post(
            executor=executor,
            path='website-frontend/v1/officalWebGuild/getGuildTaskList',
            payload={'uuid': str(executor.get('cms_guild_sid') or executor.get('cms_guild_id') or '').strip()},
            timeout_seconds=float(executor.get('request_timeout_seconds') or 30),
        )
        data = body.get('data') if isinstance(body, dict) else None
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ('list', 'records', 'items', 'tasks'):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [item for item in rows if isinstance(item, dict)]
        return []

    def _materialize_timo_revenue_incrementally(
        self,
        *,
        parent_run_id: str,
        executor: Dict[str, Any],
        executor_key: str,
        guild_name: str,
        country: str,
        target_date: date,
        provisional: int,
        snapshot_at: str,
        use_revenue_cache: bool,
        join_time_by_timo_id: Dict[str, str],
        user: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        sync_id = create_id('timo_revenue_sync')
        lock_key = f'timo_sync:{executor_key}:{target_date.isoformat()}'
        lease = TimoDbSyncLease(
            self.db.connect,
            lock_key=lock_key,
            owner_sync_id=sync_id,
            ttl_seconds=int(os.getenv('TIMO_SYNC_LOCK_TTL_SECONDS') or 600),
            auto_renew=str(getattr(self.db, 'db_path', '')) != ':memory:',
        )
        try:
            lease.acquire()
        except TimoSyncLockBusy:
            raise
        try:
            conn = self.db.connect()
            check_timo_circuit_breaker(
                conn,
                guild_executor_key=executor_key,
                sync_id=sync_id,
            )
            if provisional:
                revenue_content = self._fetch_timo_revenue_export_content_unchecked(
                    executor=executor,
                    date_from_bj=target_date.isoformat(),
                    date_to_bj=target_date.isoformat(),
                    time_type='',
                )
                self._store_timo_account_registration_times(
                    executor_key=executor_key,
                    registration_time_by_timo_id=self._timo_account_registration_times_from_revenue_content(
                        revenue_content
                    ),
                )
            else:
                revenue_content, _ = self.export_timo_guild_revenue_xlsx(
                    guild_name=guild_name,
                    user=user or {'role': OPS_AUTH_ROLE_SUPER_ADMIN},
                    export_type='day',
                    date_bj=target_date.isoformat(),
                    use_cache=use_revenue_cache,
                    join_time_by_timo_id=join_time_by_timo_id,
                )
            revenue_rows = self._parse_timo_revenue_detail_rows(revenue_content)
            source_provenance = {
                'source_business_date_bj': target_date.isoformat(),
                'normalized_stat_date_bj': target_date.isoformat(),
                'fetched_at': snapshot_at,
                'raw_response_sha256': hashlib.sha256(revenue_content).hexdigest(),
                'raw_response_bytes': len(revenue_content),
                'source_kind': 'timo_guild_revenue_xlsx',
            }
            revenue_ids = [str(row.get('timo_id') or '').strip() for row in revenue_rows]
            if not revenue_rows and not provisional:
                raise TimoIncrementalSyncError(
                    'source_not_ready',
                    f'source_not_ready:{guild_name}:{target_date.isoformat()}:empty_effective_revenue',
                )
            if not revenue_rows or any(not timo_id for timo_id in revenue_ids):
                raise RuntimeError(
                    f'timo_revenue_export_empty_or_invalid:{guild_name}:{target_date.isoformat()}'
                )
            if len(set(revenue_ids)) != len(revenue_ids):
                raise RuntimeError(
                    f'timo_revenue_duplicate_streamer_id:{guild_name}:{target_date.isoformat()}'
                )
            normalized_rows = [
                {
                    **row,
                    'quality_host': self._timo_bool_flag(row.get('quality_host')),
                }
                for row in revenue_rows
            ]
            result = materialize_timo_revenue_snapshot(
                self.db.connect,
                sync_id=sync_id,
                parent_run_id=parent_run_id,
                guild_executor_key=executor_key,
                guild_name=guild_name,
                country=country,
                stat_date_bj=target_date.isoformat(),
                provisional=bool(provisional),
                revenue_rows=normalized_rows,
                snapshot_at=snapshot_at,
                idempotency_key=sync_id,
                min_row_ratio=float(os.getenv('TIMO_SYNC_MIN_ROW_RATIO') or 0.5),
                min_income_ratio=float(os.getenv('TIMO_SYNC_MIN_INCOME_RATIO') or 0.5),
                source_provenance=source_provenance,
            )
            record_timo_circuit_success(
                self.db.connect(),
                guild_executor_key=executor_key,
            )
            return {
                **result,
                'revenue_count': len(normalized_rows),
            }
        except Exception as exc:
            error_code = str(
                getattr(exc, 'code', '')
                or getattr(exc, 'detail', '')
                or 'timo_revenue_sync_failed'
            )[:120]
            if not isinstance(exc, TimoSyncLockBusy):
                conn = self.db.connect()
                is_source_not_ready = (
                    isinstance(exc, TimoIncrementalSyncError)
                    and str(getattr(exc, 'code', '') or '') == 'source_not_ready'
                )
                is_source_quality_failure = (
                    isinstance(exc, TimoIncrementalSyncError)
                    and str(getattr(exc, 'code', '') or '').startswith('quality_gate_')
                )
                if not isinstance(exc, TimoCircuitOpen) and not is_source_quality_failure and not is_source_not_ready:
                    record_timo_circuit_failure(
                        conn,
                        guild_executor_key=executor_key,
                        error_code=error_code,
                    )
                prior = conn.execute(
                    """
                    SELECT COALESCE(MAX(retry_attempt), 0) AS attempt
                    FROM timo_sync_run_log
                    WHERE guild_executor_key=? AND stat_date_bj=? AND sync_id<>?
                    """,
                    (executor_key, target_date.isoformat(), sync_id),
                ).fetchone()
                retry_attempt = int(prior['attempt'] or 0) + 1
                existing = conn.execute(
                    "SELECT 1 FROM timo_sync_run_log WHERE sync_id=?",
                    (sync_id,),
                ).fetchone()
                if existing:
                    schedule_timo_sync_retry(
                        conn,
                        sync_id=sync_id,
                        attempt=retry_attempt,
                        persistent_retry=is_source_not_ready,
                    )
                else:
                    record_timo_sync_attempt_failure(
                        conn,
                        sync_id=sync_id,
                        parent_run_id=parent_run_id,
                        guild_executor_key=executor_key,
                        guild_name=guild_name,
                        country=country,
                        stat_date_bj=target_date.isoformat(),
                        provisional=bool(provisional),
                        error_code=error_code,
                        error=str(getattr(exc, 'detail', exc)),
                        retry_attempt=retry_attempt,
                        persistent_retry=is_source_not_ready,
                    )
            raise
        finally:
            lease.release()

    def materialize_timo_external_feed_snapshot(
        self,
        *,
        data_date_bj: Optional[str] = None,
        include_today: bool = True,
        use_revenue_cache: bool = True,
        refresh_streamers: bool = True,
        refresh_tasks: bool = True,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        target_date = self._timo_parse_revenue_date_bj(data_date_bj) if str(data_date_bj or '').strip() else now_bj.date()
        latest_complete = self._timo_revenue_latest_complete_day_bj()
        if target_date > latest_complete and not include_today:
            raise HTTPException(status_code=400, detail=f'timo_data_date_not_ready:{latest_complete.isoformat()}')
        provisional = 1 if target_date > latest_complete else 0
        snapshot_at = utc_now()
        run_id = create_id('timo_external_sync')
        guild_count = streamer_count = revenue_count = task_count = 0
        errors: List[str] = []
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO timo_external_sync_runs (
                    run_id, snapshot_at, data_date_bj, status, guild_count, streamer_count,
                    revenue_count, task_count, error, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 0, 0, 0, 0, '', ?, ?)
                """,
                (run_id, snapshot_at, target_date.isoformat(), snapshot_at, snapshot_at),
            )
            conn.commit()
        for executor in self._list_enabled_timo_guild_anchor_executors():
            guild_name = str(executor.get('guild_name') or '').strip()
            if not guild_name:
                continue
            guild_count += 1
            executor_key = self._guild_anchor_executor_key(executor)
            country = str(executor.get('country') or '').strip()
            try:
                streamers: List[Dict[str, Any]] = []
                join_time_by_timo_id: Dict[str, str] = {}
                if refresh_streamers:
                    streamers, _, join_time_by_timo_id = self._fetch_timo_complete_streamer_snapshot(executor)
                    with self.db.connect() as conn:
                        for item in streamers:
                            timo_id = self._timo_export_numeric_id(item.get('userId') or item.get('user_id') or item.get('timoId') or item.get('sid'))
                            if not timo_id:
                                continue
                            conn.execute(
                                """
                                INSERT INTO timo_external_streamers (
                                    guild_executor_key, guild_name, country, guild_country,
                                    timo_country_name, timo_id, user_uuid, nickname,
                                    registered_at_bj, joined_guild_at_bj, last_active_at_bj,
                                    is_real_person, status, host_role,
                                    source_payload, snapshot_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(guild_executor_key, timo_id) DO UPDATE SET
                                    guild_name=excluded.guild_name,
                                    country=excluded.country,
                                    guild_country=excluded.guild_country,
                                    timo_country_name=CASE
                                        WHEN excluded.timo_country_name='' THEN timo_external_streamers.timo_country_name
                                        ELSE excluded.timo_country_name
                                    END,
                                    user_uuid=excluded.user_uuid,
                                    nickname=excluded.nickname,
                                    registered_at_bj=CASE
                                        WHEN excluded.registered_at_bj='' THEN timo_external_streamers.registered_at_bj
                                        ELSE excluded.registered_at_bj
                                    END,
                                    joined_guild_at_bj=CASE
                                        WHEN excluded.joined_guild_at_bj='' THEN timo_external_streamers.joined_guild_at_bj
                                        ELSE excluded.joined_guild_at_bj
                                    END,
                                    last_active_at_bj=excluded.last_active_at_bj,
                                    is_real_person=excluded.is_real_person,
                                    status=excluded.status,
                                    host_role=excluded.host_role,
                                    source_payload=excluded.source_payload,
                                    snapshot_at=excluded.snapshot_at,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    executor_key, guild_name, country, country,
                                    str(item.get('countryName') or '').strip(),
                                    timo_id,
                                    str(item.get('userUuid') or item.get('user_uuid') or '').strip(),
                                    str(item.get('nickName') or item.get('nickname') or '').strip(),
                                    self._timo_epoch_ms_to_bj_text(item.get('joinTime') or item.get('created_at')),
                                    self._timo_epoch_ms_to_bj_text(item.get('joinTime') or item.get('created_at')),
                                    self._timo_epoch_ms_to_bj_text(item.get('lastActiveTime') or item.get('last_active_at')),
                                    self._guild_anchor_is_real_person(item),
                                    str(item.get('status') or item.get('accountStatus') or '').strip(),
                                    str(item.get('showHostRole') or item.get('hostRole') or '').strip(),
                                    json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
                                    snapshot_at, snapshot_at,
                                ),
                            )
                            streamer_count += 1
                        conn.commit()
                try:
                    revenue_result = self._materialize_timo_revenue_incrementally(
                        parent_run_id=run_id,
                        executor=executor,
                        executor_key=executor_key,
                        guild_name=guild_name,
                        country=country,
                        target_date=target_date,
                        provisional=provisional,
                        snapshot_at=snapshot_at,
                        use_revenue_cache=use_revenue_cache,
                        join_time_by_timo_id=join_time_by_timo_id,
                        user=user,
                    )
                    revenue_count += int(revenue_result.get('revenue_count') or 0)
                except Exception as exc:
                    errors.append(f'{guild_name}:revenue:{str(getattr(exc, "detail", exc))[:180]}')
                try:
                    if not refresh_tasks:
                        continue
                    tasks = self._fetch_timo_guild_task_rows(executor=executor)
                    with self.db.connect() as conn:
                        for index, task in enumerate(tasks[:8]):
                            main_task = str(task.get('mainTask') or task.get('taskUuid') or task.get('groupId') or index).strip()
                            task_type = 'quality_host' if main_task == '10001' or index == 1 else 'guild_task'
                            if task_type == 'guild_task' and index:
                                task_type = f'guild_task_{index}'
                            conn.execute(
                                """
                                INSERT INTO timo_external_guild_task_snapshots (
                                    guild_executor_key, guild_name, country, snapshot_at, task_type, task_name,
                                    target_diamonds, progress_diamonds, reward_diamonds, task_status,
                                    source_payload, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(guild_executor_key, snapshot_at, task_type) DO UPDATE SET
                                    guild_name=excluded.guild_name,
                                    country=excluded.country,
                                    task_name=excluded.task_name,
                                    target_diamonds=excluded.target_diamonds,
                                    progress_diamonds=excluded.progress_diamonds,
                                    reward_diamonds=excluded.reward_diamonds,
                                    task_status=excluded.task_status,
                                    source_payload=excluded.source_payload,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    executor_key, guild_name, country, snapshot_at, task_type,
                                    str(task.get('taskName') or task.get('name') or '').strip(),
                                    self._timo_revenue_number(task.get('taskTarget')),
                                    self._timo_revenue_number(task.get('taskProgress')),
                                    self._timo_task_reward_diamonds(task),
                                    str(task.get('taskStatus') or '').strip(),
                                    json.dumps(task, ensure_ascii=False, sort_keys=True, default=str),
                                    snapshot_at,
                                ),
                            )
                            task_count += 1
                        conn.commit()
                except Exception as exc:
                    errors.append(f'{guild_name}:tasks:{str(getattr(exc, "detail", exc))[:180]}')
            except Exception as exc:
                errors.append(f'{guild_name}:streamers:{str(getattr(exc, "detail", exc))[:180]}')
        status = 'partial' if errors else 'success'
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE timo_external_sync_runs
                SET status=?, guild_count=?, streamer_count=?, revenue_count=?, task_count=?,
                    error=?, updated_at=?
                WHERE run_id=?
                """,
                (status, guild_count, streamer_count, revenue_count, task_count, '; '.join(errors)[:1200], utc_now(), run_id),
            )
            conn.commit()
        return {
            'ok': not errors,
            'run_id': run_id,
            'status': status,
            'snapshot_at': snapshot_at,
            'data_date_bj': target_date.isoformat(),
            'provisional': bool(provisional),
            'guild_count': guild_count,
            'streamer_count': streamer_count,
            'revenue_count': revenue_count,
            'task_count': task_count,
            'errors': errors,
        }

    @staticmethod
    def _timo_external_limit_offset(limit: int, offset: int) -> Tuple[int, int]:
        safe_limit = max(1, min(5000, int(limit or 500)))
        safe_offset = max(0, int(offset or 0))
        return safe_limit, safe_offset

    @staticmethod
    def _timo_external_country(value: Any) -> str:
        raw = str(value or '').strip()
        if not raw:
            return ''
        normalized = normalize_country_label(raw)
        if normalized not in {'Mexico', 'Indonesia', 'Brazil'}:
            raise HTTPException(
                status_code=400,
                detail={
                    'ok': False,
                    'reason': 'unsupported_country',
                    'supported_countries': ['Mexico', 'Indonesia', 'Brazil'],
                },
            )
        return normalized

    @staticmethod
    def _timo_external_guild_storage_name(
        *, guild_name: str = '', guild_id: str = '', guild_sid: str = ''
    ) -> str:
        if not any(str(value or '').strip() for value in (guild_name, guild_id, guild_sid)):
            return ''
        try:
            return require_timo_guild_identity(
                guild_name,
                guild_id=guild_id,
                guild_sid=guild_sid,
            ).storage_name
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    'ok': False,
                    'reason': str(exc),
                    'message': 'guild_id、guild_sid 与 guild_name 必须指向同一 Timo 公会',
                },
            ) from exc

    def list_timo_external_countries(self) -> Dict[str, Any]:
        country_rows: Dict[str, Dict[str, Any]] = {
            country: {
                'country': country,
                'country_code': code,
                'available': False,
                'guild_names': [],
                'streamer_count': 0,
                'revenue_row_count': 0,
                'revenue_min_date_bj': '',
                'revenue_max_date_bj': '',
                'latest_snapshot_at': '',
            }
            for country, code in (('Mexico', 'MX'), ('Indonesia', 'ID'), ('Brazil', 'BR'))
        }
        with self.db.connect() as conn:
            guild_rows = conn.execute(
                """
                SELECT country, guild_name FROM timo_external_streamers
                UNION
                SELECT country, guild_name FROM timo_external_revenue_daily
                UNION
                SELECT country, guild_name FROM timo_external_guild_task_snapshots
                ORDER BY country, guild_name
                """
            ).fetchall()
            streamer_rows = conn.execute(
                """
                SELECT country, COUNT(*) AS row_count, MAX(snapshot_at) AS latest_snapshot_at
                FROM timo_external_streamers
                GROUP BY country
                """
            ).fetchall()
            revenue_rows = conn.execute(
                """
                SELECT country, COUNT(*) AS row_count, MIN(stat_date_bj) AS min_date_bj,
                       MAX(stat_date_bj) AS max_date_bj, MAX(snapshot_at) AS latest_snapshot_at
                FROM timo_external_revenue_daily
                GROUP BY country
                """
            ).fetchall()
            task_rows = conn.execute(
                """
                SELECT country, MAX(snapshot_at) AS latest_snapshot_at
                FROM timo_external_guild_task_snapshots
                GROUP BY country
                """
            ).fetchall()
        for row in guild_rows:
            country = normalize_country_label(row['country'])
            guild_name = str(row['guild_name'] or '').strip()
            if country in country_rows and guild_name and guild_name not in country_rows[country]['guild_names']:
                country_rows[country]['guild_names'].append(guild_name)
        for row in streamer_rows:
            country = normalize_country_label(row['country'])
            if country not in country_rows:
                continue
            country_rows[country]['streamer_count'] = int(row['row_count'] or 0)
            country_rows[country]['latest_snapshot_at'] = str(row['latest_snapshot_at'] or '')
        for row in revenue_rows:
            country = normalize_country_label(row['country'])
            if country not in country_rows:
                continue
            country_rows[country]['revenue_row_count'] = int(row['row_count'] or 0)
            country_rows[country]['revenue_min_date_bj'] = str(row['min_date_bj'] or '')
            country_rows[country]['revenue_max_date_bj'] = str(row['max_date_bj'] or '')
            country_rows[country]['latest_snapshot_at'] = max(
                country_rows[country]['latest_snapshot_at'],
                str(row['latest_snapshot_at'] or ''),
            )
        for row in task_rows:
            country = normalize_country_label(row['country'])
            if country not in country_rows:
                continue
            country_rows[country]['latest_snapshot_at'] = max(
                country_rows[country]['latest_snapshot_at'],
                str(row['latest_snapshot_at'] or ''),
            )
        rows = list(country_rows.values())
        for row in rows:
            contracts = [
                timo_guild_contract_fields(name)
                for name in row['guild_names']
            ]
            contracts = [contract for contract in contracts if contract]
            row['guilds'] = contracts
            row['guild_ids'] = [contract['guild_id'] for contract in contracts]
            row['guild_storage_names'] = [contract['guild_storage_name'] for contract in contracts]
            row['guild_names'] = [contract['guild_name'] for contract in contracts]
            row['available'] = bool(contracts)
        return {
            'ok': True,
            'system_version': 'mcn_timo_external_feed_v2',
            'schema_version': 'timo_external_identity_v1',
            'identity_key': 'guild_id',
            'total': len(rows),
            'rows': rows,
        }

    def list_timo_external_streamers(
        self,
        *,
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        updated_since: str = '',
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        safe_limit, safe_offset = self._timo_external_limit_offset(limit, offset)
        where: List[str] = []
        params: List[Any] = []
        normalized_country = self._timo_external_country(country)
        if normalized_country:
            where.append("COALESCE(NULLIF(guild_country, ''), country) = ?")
            params.append(normalized_country)
        normalized_guild_name = self._timo_external_guild_storage_name(
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
        )
        if normalized_guild_name:
            where.append('guild_name = ?')
            params.append(normalized_guild_name)
        if str(updated_since or '').strip():
            where.append('updated_at >= ?')
            params.append(str(updated_since or '').strip())
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db.connect() as conn:
            total = int(conn.execute(f'SELECT COUNT(*) AS n FROM timo_external_streamers{where_sql}', tuple(params)).fetchone()['n'] or 0)
            rows = [dict(row) for row in conn.execute(
                f"""
                SELECT guild_name, country,
                       COALESCE(NULLIF(guild_country, ''), country) AS guild_country,
                       timo_country_name,
                       timo_id, user_uuid, nickname,
                       COALESCE(NULLIF(joined_guild_at_bj, ''), registered_at_bj) AS joined_guild_at_bj,
                       timo_registered_at_bj,
                       last_active_at_bj, is_real_person, status, host_role, snapshot_at, updated_at
                FROM timo_external_streamers{where_sql}
                ORDER BY updated_at DESC, guild_name ASC, timo_id ASC
                LIMIT ? OFFSET ?
                """,
                tuple(params + [safe_limit, safe_offset]),
            ).fetchall()]
        return externalize_timo_guild_names({'ok': True, 'system_version': 'mcn_timo_external_feed_v2', 'schema_version': 'timo_external_v3', 'identity_key': 'guild_id', 'total': total, 'limit': safe_limit, 'offset': safe_offset, 'rows': rows})

    def list_timo_external_revenue_daily(
        self,
        *,
        stat_date_bj: str = '',
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        updated_since: str = '',
        include_provisional: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        normalized_stat_date = str(stat_date_bj or '').strip()
        if not normalized_stat_date:
            raise HTTPException(status_code=400, detail={'ok': False, 'reason': 'stat_date_required'})
        safe_limit, safe_offset = self._timo_external_limit_offset(limit, offset)
        where: List[str] = []
        params: List[Any] = []
        where.append('stat_date_bj = ?')
        params.append(normalized_stat_date)
        normalized_country = self._timo_external_country(country)
        if normalized_country:
            where.append('country = ?')
            params.append(normalized_country)
        normalized_guild_name = self._timo_external_guild_storage_name(
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
        )
        if normalized_guild_name:
            where.append('guild_name = ?')
            params.append(normalized_guild_name)
        if str(updated_since or '').strip():
            where.append('updated_at >= ?')
            params.append(str(updated_since or '').strip())
        if not include_provisional:
            where.append('provisional = 0')
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db.connect() as conn:
            total = int(conn.execute(f'SELECT COUNT(*) AS n FROM timo_external_revenue_daily{where_sql}', tuple(params)).fetchone()['n'] or 0)
            rows = [dict(row) for row in conn.execute(
                f"""
                SELECT guild_name, country, stat_date_bj, timo_id, user_uuid, nickname,
                       total_income, qualified_revenue, matching_income, private_message_income,
                       private_gift_income, call_income, online_hours, call_count, quality_host,
                       quality_revenue, provisional, revision_version, last_sync_id,
                       snapshot_at, updated_at
                FROM timo_external_revenue_daily{where_sql}
                ORDER BY stat_date_bj DESC, updated_at DESC, guild_name ASC, timo_id ASC
                LIMIT ? OFFSET ?
                """,
                tuple(params + [safe_limit, safe_offset]),
            ).fetchall()]
            feed_status = timo_external_feed_status(
                conn,
                stat_date_bj=normalized_stat_date,
                country=normalized_country,
                guild_name=normalized_guild_name,
            )
        return externalize_timo_guild_names({
            'ok': True,
            'system_version': 'mcn_timo_external_feed_v2',
            'schema_version': 'timo_external_v3',
            'identity_key': 'guild_id',
            'materialization_version': 'timo_incremental_v1',
            **feed_status,
            'total': total,
            'limit': safe_limit,
            'offset': safe_offset,
            'rows': rows,
        })

    def list_timo_external_guild_tasks(
        self,
        *,
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        snapshot_since: str = '',
        include_history: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        safe_limit, safe_offset = self._timo_external_limit_offset(limit, offset)
        where: List[str] = []
        params: List[Any] = []
        normalized_country = self._timo_external_country(country)
        if normalized_country:
            where.append('country = ?')
            params.append(normalized_country)
        normalized_guild_name = self._timo_external_guild_storage_name(
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
        )
        if normalized_guild_name:
            where.append('guild_name = ?')
            params.append(normalized_guild_name)
        if str(snapshot_since or '').strip():
            where.append('snapshot_at >= ?')
            params.append(str(snapshot_since or '').strip())
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db.connect() as conn:
            if include_history:
                source_sql = f'timo_external_guild_task_snapshots{where_sql}'
                source_params = tuple(params)
            else:
                source_sql = f"""
                    (
                        SELECT t.*
                        FROM timo_external_guild_task_snapshots t
                        JOIN (
                            SELECT guild_executor_key, task_type, task_name, MAX(snapshot_at) AS latest_snapshot_at
                            FROM timo_external_guild_task_snapshots{where_sql}
                            GROUP BY guild_executor_key, task_type, task_name
                        ) latest
                          ON latest.guild_executor_key = t.guild_executor_key
                         AND latest.task_type = t.task_type
                         AND latest.task_name = t.task_name
                         AND latest.latest_snapshot_at = t.snapshot_at
                    )
                """
                source_params = tuple(params)
            total = int(conn.execute(f'SELECT COUNT(*) AS n FROM {source_sql}', source_params).fetchone()['n'] or 0)
            rows = [dict(row) for row in conn.execute(
                f"""
                SELECT guild_name, country, snapshot_at, task_type,
                       COALESCE(NULLIF(json_extract(source_payload, '$.mainTask'), ''), task_type) AS task_key,
                       task_name, target_diamonds,
                       progress_diamonds, reward_diamonds, task_status, updated_at
                FROM {source_sql}
                ORDER BY snapshot_at DESC, guild_name ASC, task_type ASC
                LIMIT ? OFFSET ?
                """,
                tuple(list(source_params) + [safe_limit, safe_offset]),
            ).fetchall()]
        return externalize_timo_guild_names({'ok': True, 'system_version': 'mcn_timo_external_feed_v2', 'schema_version': 'timo_external_v3', 'identity_key': 'guild_id', 'include_history': bool(include_history), 'total': total, 'limit': safe_limit, 'offset': safe_offset, 'rows': rows})

    def list_timo_external_sync_runs(
        self,
        *,
        status: str = 'success',
        data_date_bj: str = '',
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        safe_limit, safe_offset = self._timo_external_limit_offset(limit, offset)
        normalized_status = str(status or 'success').strip().lower()
        where: List[str] = []
        params: List[Any] = []
        if normalized_status and normalized_status != 'all':
            where.append('status = ?')
            params.append(normalized_status)
        if str(data_date_bj or '').strip():
            where.append('data_date_bj = ?')
            params.append(str(data_date_bj or '').strip())
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db.connect() as conn:
            total = int(conn.execute(f'SELECT COUNT(*) AS n FROM timo_external_sync_runs{where_sql}', tuple(params)).fetchone()['n'] or 0)
            rows = [dict(row) for row in conn.execute(
                f"""
                SELECT run_id, snapshot_at, data_date_bj, status, guild_count, streamer_count,
                       revenue_count, task_count, error, created_at, updated_at
                FROM timo_external_sync_runs{where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params + [safe_limit, safe_offset]),
            ).fetchall()]
        return {'ok': True, 'system_version': 'mcn_timo_external_feed_v1', 'schema_version': 'timo_external_v1', 'total': total, 'limit': safe_limit, 'offset': safe_offset, 'rows': rows}

    def list_timo_incremental_sync_runs(
        self,
        *,
        status: str = 'all',
        data_date_bj: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        safe_limit, safe_offset = self._timo_external_limit_offset(limit, offset)
        where: List[str] = []
        params: List[Any] = []
        normalized_status = str(status or 'all').strip().lower()
        if normalized_status and normalized_status != 'all':
            where.append('status=?')
            params.append(normalized_status)
        if str(data_date_bj or '').strip():
            where.append('stat_date_bj=?')
            params.append(str(data_date_bj).strip())
        normalized_guild_name = self._timo_external_guild_storage_name(
            guild_name=guild_name,
            guild_id=guild_id,
            guild_sid=guild_sid,
        )
        if normalized_guild_name:
            where.append('guild_name=?')
            params.append(normalized_guild_name)
        where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
        with self.db.connect() as conn:
            total = int(conn.execute(
                f'SELECT COUNT(*) AS n FROM timo_sync_run_log{where_sql}',
                tuple(params),
            ).fetchone()['n'] or 0)
            rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT sync_id, parent_run_id, guild_name, country, stat_date_bj,
                           data_status, start_time, end_time, status, error_code,
                           row_count, old_row_count, inserted_count, updated_count,
                           deleted_count, unchanged_count, checksum, duration_ms,
                           gate_evidence_json, diff_evidence_json, rollback_of_sync_id,
                           rolled_back_at, retry_attempt, next_retry_at
                    FROM timo_sync_run_log{where_sql}
                    ORDER BY start_time DESC
                    LIMIT ? OFFSET ?
                    """,
                    tuple(params + [safe_limit, safe_offset]),
                ).fetchall()
            ]
        for row in rows:
            for field in ('gate_evidence_json', 'diff_evidence_json'):
                raw = str(row.pop(field, '') or '')
                try:
                    row[field.removesuffix('_json')] = json.loads(raw) if raw else {}
                except Exception:
                    row[field.removesuffix('_json')] = {}
            provenance = row.get('gate_evidence', {}).get('source_provenance', {})
            row['source_business_date_bj'] = str(provenance.get('source_business_date_bj') or '')
            row['normalized_stat_date_bj'] = str(provenance.get('normalized_stat_date_bj') or row.get('stat_date_bj') or '')
            row['fetched_at'] = str(provenance.get('fetched_at') or row.get('start_time') or '')
            row['raw_response_sha256'] = str(provenance.get('raw_response_sha256') or '')
            row['raw_response_bytes'] = int(provenance.get('raw_response_bytes') or 0)
        return externalize_timo_guild_names({
            'ok': True,
            'system_version': 'mcn_timo_external_feed_v2',
            'schema_version': 'timo_incremental_evidence_v2',
            'identity_key': 'guild_id',
            'total': total,
            'limit': safe_limit,
            'offset': safe_offset,
            'rows': rows,
        })

    def list_timo_bi_revenue_daily(
        self,
        *,
        stat_date_bj: str,
        country: str = '',
        guild_name: str = '',
        guild_id: str = '',
        guild_sid: str = '',
        updated_since: str = '',
        include_provisional: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> Dict[str, Any]:
        normalized_stat_date = str(stat_date_bj or '').strip()
        if not normalized_stat_date:
            raise HTTPException(status_code=400, detail={'ok': False, 'reason': 'stat_date_required'})
        normalized_country = self._timo_external_country(country)
        mart_path = str(
            os.getenv('TIMO_BI_MART_DB_PATH')
            or (Path(self.db.db_path).resolve().parent / 'timo_bi_mart.db')
        )
        try:
            return externalize_timo_guild_names(query_timo_bi_mart(
                mart_db_path=mart_path,
                stat_date_bj=normalized_stat_date,
                country=normalized_country,
                guild_name=self._timo_external_guild_storage_name(
                    guild_name=guild_name,
                    guild_id=guild_id,
                    guild_sid=guild_sid,
                ),
                updated_since=str(updated_since or '').strip(),
                include_provisional=include_provisional,
                limit=limit,
                offset=offset,
                statement_timeout_ms=int(os.getenv('TIMO_BI_STATEMENT_TIMEOUT_MS') or 2000),
            ))
        except TimoBiMartQueryTimeout as exc:
            raise HTTPException(
                status_code=504,
                detail={'ok': False, 'reason': str(exc)},
            ) from exc
        except TimoBiMartError as exc:
            raise HTTPException(
                status_code=503,
                detail={'ok': False, 'reason': str(exc)},
            ) from exc

    def export_timo_first_20k_diamonds_xlsx(self, *, guild_name: str, user: Optional[Dict[str, Any]], as_of_date_bj: Optional[str] = None) -> Tuple[bytes, str]:
        normalized_guild = str(guild_name or '').strip()
        target_date = self._timo_resolve_latest_complete_date_bj(as_of_date_bj)
        executor = self._timo_resolve_export_executor(guild_name=normalized_guild, user=user)
        qualified_rows = self._timo_cached_anchor_export_rows(executor=executor, export_kind='first_20k_diamonds', data_date_bj=target_date.isoformat())
        if not qualified_rows and not ((self._timo_export_cache_status_for_executor(executor).get('first_20k_diamonds') or {}).get('ready')):
            if not str(executor.get('cms_guild_id') or executor.get('cms_guild_sid') or '').strip():
                raise HTTPException(status_code=400, detail='timo_guild_lock_required')
            raise HTTPException(status_code=404, detail=f'timo_anchor_export_cache_not_ready:first_20k_diamonds:{target_date.isoformat()}')
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '首次2万钻'
        sheet.append(['主播id', '主播名', '钻石数量'])
        for row in qualified_rows:
            sheet.append([
                str(row.get('timo_id') or '').strip(),
                str(row.get('anchor_name') or '').strip(),
                self._timo_revenue_format(float(row.get('diamond_amount') or 0.0)),
            ])
        self._timo_export_style_sheet(sheet)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue(), self.timo_first_20k_diamonds_export_filename(guild_name=normalized_guild, date_bj=target_date.isoformat())

    def timo_first_20k_diamonds_export_filename(self, *, guild_name: str, date_bj: Optional[str] = None) -> str:
        display_guild = self._timo_intake_guild_display_name(str(guild_name or '').strip())
        safe_guild = re.sub(r'[^A-Za-z0-9]+', '', display_guild) or 'Timo'
        raw_date = str(date_bj or '').strip()
        compact_date = re.sub(r'\D+', '', raw_date)[-6:] if raw_date else ''
        compact_date = compact_date or datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%y%m%d')
        return f'{safe_guild}First20kDiamonds{compact_date}.xlsx'

    def _timo_revenue_export_range_bj(self, period: str, today_bj: Optional[datetime.date] = None) -> Tuple[str, str]:
        normalized_period = str(period or '').strip().lower()
        if normalized_period == 'yesterday':
            target = (today_bj - timedelta(days=1)) if today_bj else self._timo_revenue_latest_complete_day_bj()
            return target.isoformat(), target.isoformat()
        if normalized_period == 'last_week':
            if today_bj:
                this_week_monday = today_bj - timedelta(days=today_bj.weekday())
                end = this_week_monday - timedelta(days=1)
            else:
                latest_day = self._timo_revenue_latest_complete_day_bj()
                end = latest_day - timedelta(days=(latest_day.weekday() + 1) % 7)
            start = end - timedelta(days=6)
            return start.isoformat(), end.isoformat()
        raise HTTPException(status_code=400, detail='unsupported_timo_revenue_export_period')

    def _timo_revenue_cache_file_path(
        self,
        *,
        executor: Dict[str, Any],
        export_type: str,
        date_from_bj: str,
        date_to_bj: str,
        filename: str,
    ) -> Path:
        executor_key = self._guild_anchor_executor_key(executor)
        safe_executor = re.sub(r'[^A-Za-z0-9_.-]+', '_', executor_key).strip('_') or 'timo'
        safe_filename = re.sub(r'[^A-Za-z0-9_.-]+', '_', filename).strip('_') or 'timo_revenue.xlsx'
        cache_root = Path(self.db.db_path).parent if str(self.db.db_path or '') != ':memory:' else Path(tempfile.gettempdir()) / 'mcn_timo_revenue_exports'
        cache_dir = cache_root / 'timo_revenue_exports' / safe_executor / str(export_type or 'day')
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f'{date_from_bj}_{date_to_bj}_{safe_filename}'

    def _load_timo_revenue_export_cache(
        self,
        *,
        executor: Dict[str, Any],
        export_type: str,
        date_from_bj: str,
        date_to_bj: str,
    ) -> Optional[Tuple[bytes, str]]:
        executor_key = self._guild_anchor_executor_key(executor)
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT filename, file_path, status
                FROM timo_revenue_export_cache
                WHERE guild_executor_key = ?
                  AND export_type = ?
                  AND date_from_bj = ?
                  AND date_to_bj = ?
                """,
                (executor_key, str(export_type or '').strip(), str(date_from_bj or '').strip(), str(date_to_bj or '').strip()),
            ).fetchone()
        if not row or str(row['status'] or '') != 'success':
            return None
        file_path = Path(str(row['file_path'] or ''))
        if not file_path.exists() or not file_path.is_file():
            return None
        content = file_path.read_bytes()
        mismatch_errors = self._timo_revenue_cached_export_mismatches(content)
        if mismatch_errors:
            error_text = 'timo_revenue_cache_detail_mismatch:' + ';'.join(mismatch_errors[:3])
            now_iso = utc_now()
            with self.db.connect() as conn:
                conn.execute(
                    """
                    UPDATE timo_revenue_export_cache
                    SET status = 'invalid', error = ?, updated_at = ?
                    WHERE guild_executor_key = ?
                      AND export_type = ?
                      AND date_from_bj = ?
                      AND date_to_bj = ?
                    """,
                    (error_text[:500], now_iso, executor_key, str(export_type or '').strip(), str(date_from_bj or '').strip(), str(date_to_bj or '').strip()),
                )
                conn.commit()
            return None
        return content, str(row['filename'] or file_path.name)

    def _timo_export_cache_status_for_executor(self, executor: Dict[str, Any]) -> Dict[str, Any]:
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        latest_day = self._timo_revenue_latest_complete_day_bj()
        yesterday_from, yesterday_to = self._timo_revenue_export_range_bj('yesterday')
        week_from, week_to = self._timo_revenue_export_range_bj('last_week')
        anchor_state_by_kind: Dict[str, Dict[str, Any]] = {}
        for path in TIMO_ANCHOR_EXPORT_CACHE_STATUS_PATHS:
            try:
                if not path.exists():
                    continue
                payload = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            updated_at = str((payload or {}).get('updated_at') or '')
            for item in (payload.get('results') if isinstance(payload, dict) else []) or []:
                if not isinstance(item, dict) or not bool(item.get('ok')):
                    continue
                if str(item.get('guild_name') or '').strip() != guild_name:
                    continue
                result = item.get('result') if isinstance(item.get('result'), dict) else {}
                nested = result.get('results') if isinstance(result.get('results'), dict) else {}
                for kind in ('real_person', 'first_20k_diamonds'):
                    info = nested.get(kind)
                    if not isinstance(info, dict) or not bool(info.get('ok')):
                        continue
                    data_date = str(info.get('data_date_bj') or '').strip()
                    if not data_date:
                        continue
                    current = anchor_state_by_kind.get(kind) or {}
                    if data_date >= str(current.get('data_date_bj') or ''):
                        anchor_state_by_kind[kind] = {
                            'export_kind': kind,
                            'data_date_bj': data_date,
                            'row_count': int(info.get('candidate_count') or 0),
                            'updated_at': updated_at,
                            'ready': True,
                        }
        with self.db.connect() as conn:
            revenue_rows = conn.execute(
                """
                SELECT export_type, date_from_bj, date_to_bj, filename, file_size, updated_at
                FROM timo_revenue_export_cache
                WHERE guild_executor_key = ?
                  AND status = 'success'
                  AND (
                    (export_type = 'day' AND date_from_bj = ? AND date_to_bj = ?)
                    OR (export_type = 'week' AND date_from_bj = ? AND date_to_bj = ?)
                  )
                """,
                (executor_key, yesterday_from, yesterday_to, week_from, week_to),
            ).fetchall()
            anchor_rows = conn.execute(
                """
                SELECT export_kind, data_date_bj, COUNT(*) AS row_count, MAX(updated_at) AS updated_at
                FROM timo_anchor_export_cache
                WHERE guild_executor_key = ?
                  AND export_kind IN ('real_person', 'first_20k_diamonds')
                  AND data_date_bj = ?
                GROUP BY export_kind, data_date_bj
                """,
                (executor_key, latest_day.isoformat()),
            ).fetchall()
            latest_revenue_rows = conn.execute(
                """
                SELECT export_type, date_from_bj, date_to_bj, filename, file_size, updated_at
                FROM timo_revenue_export_cache
                WHERE guild_executor_key = ?
                  AND status = 'success'
                  AND export_type IN ('day', 'week')
                ORDER BY date_to_bj DESC, updated_at DESC
                """,
                (executor_key,),
            ).fetchall()
            latest_anchor_rows = conn.execute(
                """
                SELECT export_kind, data_date_bj, COUNT(*) AS row_count, MAX(updated_at) AS updated_at
                FROM timo_anchor_export_cache
                WHERE guild_executor_key = ?
                  AND export_kind IN ('real_person', 'first_20k_diamonds')
                GROUP BY export_kind, data_date_bj
                ORDER BY data_date_bj DESC, updated_at DESC
                """,
                (executor_key,),
            ).fetchall()
        revenue_by_type = {str(row['export_type'] or ''): dict(row) for row in revenue_rows}
        anchor_by_kind = {str(row['export_kind'] or ''): dict(row) for row in anchor_rows}
        latest_revenue_by_type: Dict[str, Dict[str, Any]] = {}
        for row in latest_revenue_rows:
            export_type = str(row['export_type'] or '')
            if export_type and export_type not in latest_revenue_by_type:
                latest_revenue_by_type[export_type] = dict(row)
        latest_anchor_by_kind: Dict[str, Dict[str, Any]] = {}
        for row in latest_anchor_rows:
            export_kind = str(row['export_kind'] or '')
            if export_kind and export_kind not in latest_anchor_by_kind:
                latest_anchor_by_kind[export_kind] = dict(row)

        def revenue_status(kind: str, date_from: str, date_to: str) -> Dict[str, Any]:
            row = revenue_by_type.get(kind)
            latest_row = latest_revenue_by_type.get(kind) or {}
            return {
                'ready': bool(row),
                'date_from_bj': date_from,
                'date_to_bj': date_to,
                'filename': str((row or {}).get('filename') or ''),
                'file_size': int((row or {}).get('file_size') or 0),
                'updated_at': str((row or {}).get('updated_at') or ''),
                'latest_ready_date_from_bj': str(latest_row.get('date_from_bj') or ''),
                'latest_ready_date_to_bj': str(latest_row.get('date_to_bj') or ''),
                'latest_ready_updated_at': str(latest_row.get('updated_at') or ''),
            }

        def anchor_status(kind: str) -> Dict[str, Any]:
            row = anchor_by_kind.get(kind)
            latest_row = latest_anchor_by_kind.get(kind) or {}
            state_row = anchor_state_by_kind.get(kind) or {}
            if not row and str(state_row.get('data_date_bj') or '') == latest_day.isoformat():
                row = state_row
            if str(state_row.get('data_date_bj') or '') > str(latest_row.get('data_date_bj') or ''):
                latest_row = state_row
            return {
                'ready': bool(row),
                'checked': bool(row),
                'data_date_bj': latest_day.isoformat(),
                'row_count': int((row or {}).get('row_count') or 0),
                'updated_at': str((row or {}).get('updated_at') or ''),
                'latest_ready_data_date_bj': str(latest_row.get('data_date_bj') or ''),
                'latest_ready_row_count': int(latest_row.get('row_count') or 0),
                'latest_ready_updated_at': str(latest_row.get('updated_at') or ''),
            }

        return {
            'latest_complete_day_bj': latest_day.isoformat(),
            'last_week_from_bj': week_from,
            'last_week_to_bj': week_to,
            'revenue_yesterday': revenue_status('day', yesterday_from, yesterday_to),
            'revenue_last_week': revenue_status('week', week_from, week_to),
            'real_person': anchor_status('real_person'),
            'first_20k_diamonds': anchor_status('first_20k_diamonds'),
        }

    def _timo_weekly_export_cache_due(self, now_bj: datetime) -> bool:
        return now_bj.weekday() > 0 or (now_bj.weekday() == 0 and (now_bj.hour > 16 or now_bj.minute >= 10))

    def _trigger_timo_export_cache_units(self, *, need_daily: bool, need_weekly: bool, force: bool = False) -> Dict[str, Any]:
        if platform.system().lower() != 'linux' or not shutil.which('systemctl'):
            return {'ok': False, 'skipped': True, 'reason': 'systemctl_unavailable'}
        if not need_daily and not need_weekly:
            return {'ok': True, 'skipped': True, 'reason': 'cache_ready'}
        state_path = PROJECT_ROOT / 'data' / 'timo_export_cache_catchup_state.json'
        try:
            state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        except Exception:
            state = {}
        now_ts = time.time()
        started: List[str] = []
        skipped: Dict[str, str] = {}

        def trigger(unit: str) -> None:
            last_ts = float(state.get(unit) or 0)
            if not force and now_ts - last_ts < 1800:
                skipped[unit] = 'throttled'
                return
            completed = subprocess.run(['systemctl', 'is-active', '--quiet', unit], capture_output=True, timeout=3, check=False)
            if completed.returncode == 0:
                skipped[unit] = 'already_active'
                return
            subprocess.run(['systemctl', 'start', unit], capture_output=True, timeout=5, check=False)
            state[unit] = now_ts
            started.append(unit)

        try:
            if need_daily:
                trigger('mcn-timo-anchor-export-cache-daily.service')
            if need_weekly:
                trigger('mcn-timo-anchor-export-cache-weekly.service')
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding='utf-8')
            return {'ok': True, 'started': started, 'skipped_units': skipped}
        except Exception:
            return {'ok': False, 'started': started, 'skipped_units': skipped}

    def _maybe_trigger_timo_export_cache_catchup(self, statuses: List[Dict[str, Any]]) -> None:
        now_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        if now_bj.hour < 16:
            return
        need_daily = any(
            not (status.get('revenue_yesterday') or {}).get('ready')
            or not (status.get('real_person') or {}).get('ready')
            for status in statuses
        )
        need_weekly = self._timo_weekly_export_cache_due(now_bj) and any(
            not (status.get('revenue_last_week') or {}).get('ready')
            or not (status.get('first_20k_diamonds') or {}).get('ready')
            for status in statuses
        )
        self._trigger_timo_export_cache_units(need_daily=need_daily, need_weekly=need_weekly)

    def _trigger_timo_anchor_stats_catchup_after_ticket_update(self, executor: Dict[str, Any]) -> Dict[str, Any]:
        if str(executor.get('app_name') or 'timo').strip().lower() != 'timo':
            return {'ok': True, 'skipped': True, 'reason': 'not_timo_executor'}
        guild_name = str(executor.get('guild_name') or '').strip()
        if not guild_name:
            return {'ok': True, 'skipped': True, 'reason': 'missing_guild_name'}
        now_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        target_dates = [
            (now_bj.date() - timedelta(days=offset)).isoformat()
            for offset in range(1, self.guild_anchor_daily_stats_backfill_days + 1)
        ]
        try:
            result = self.enqueue_guild_anchor_daily_stat_jobs(
                stat_dates=target_dates,
                source='ticket_recovery',
                force=True,
                guild_names=[guild_name],
            )
            return {
                'ok': True,
                'guild_name': guild_name,
                'dates': result.get('dates') or target_dates,
                'job_count': int(result.get('job_count') or 0),
                'queued': True,
            }
        except Exception as exc:
            print(f'Timo anchor stats ticket recovery catchup degraded for {guild_name}: {str(exc)[:300]}')
            return {'ok': False, 'guild_name': guild_name, 'error': str(exc)[:300]}

    def _trigger_timo_export_cache_after_ticket_update(self, executor: Dict[str, Any]) -> Dict[str, Any]:
        now_bj = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        if now_bj.hour < 16:
            return {'ok': True, 'skipped': True, 'reason': 'before_daily_ready_window'}
        status = self._timo_export_cache_status_for_executor(executor)
        need_daily = (
            not (status.get('revenue_yesterday') or {}).get('ready')
            or not (status.get('real_person') or {}).get('ready')
        )
        need_weekly = self._timo_weekly_export_cache_due(now_bj) and (
            not (status.get('revenue_last_week') or {}).get('ready')
            or not (status.get('first_20k_diamonds') or {}).get('ready')
        )
        return self._trigger_timo_export_cache_units(need_daily=need_daily, need_weekly=need_weekly, force=True)

    def _store_timo_revenue_export_cache(
        self,
        *,
        executor: Dict[str, Any],
        export_type: str,
        date_from_bj: str,
        date_to_bj: str,
        content: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        executor_key = self._guild_anchor_executor_key(executor)
        guild_name = str(executor.get('guild_name') or '').strip()
        file_path = self._timo_revenue_cache_file_path(
            executor=executor,
            export_type=export_type,
            date_from_bj=date_from_bj,
            date_to_bj=date_to_bj,
            filename=filename,
        )
        file_path.write_bytes(content)
        now_iso = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO timo_revenue_export_cache (
                    guild_executor_key, guild_name, export_type, date_from_bj, date_to_bj,
                    filename, file_path, file_size, status, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'success', '', ?, ?)
                ON CONFLICT(guild_executor_key, export_type, date_from_bj, date_to_bj) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    filename = excluded.filename,
                    file_path = excluded.file_path,
                    file_size = excluded.file_size,
                    status = 'success',
                    error = '',
                    updated_at = excluded.updated_at
                """,
                (executor_key, guild_name, str(export_type or '').strip(), date_from_bj, date_to_bj, filename, str(file_path), len(content), now_iso, now_iso),
            )
            conn.commit()
        return {'file_path': str(file_path), 'filename': filename, 'file_size': len(content)}

    def materialize_timo_revenue_export_cache(
        self,
        *,
        guild_name: str,
        user: Optional[Dict[str, Any]],
        period: str,
    ) -> Dict[str, Any]:
        normalized_period = str(period or '').strip().lower()
        if normalized_period not in {'yesterday', 'last_week'}:
            raise HTTPException(status_code=400, detail='unsupported_timo_revenue_export_period')
        normalized_guild = str(guild_name or '').strip()
        executor = self._timo_resolve_export_executor(guild_name=normalized_guild, user=user)
        date_from_bj, date_to_bj = self._timo_revenue_export_range_bj(normalized_period)
        export_type = 'week' if normalized_period == 'last_week' else 'day'
        content, filename = self.export_timo_guild_revenue_xlsx(
            guild_name=normalized_guild,
            user=user,
            export_type=export_type,
            date_bj=date_to_bj,
            use_cache=False,
        )
        if normalized_period == 'yesterday' and not self._parse_timo_revenue_detail_rows(content):
            raise HTTPException(
                status_code=503,
                detail=f'source_not_ready:{normalized_guild}:{date_to_bj}:empty_effective_revenue',
            )
        stored = self._store_timo_revenue_export_cache(
            executor=executor,
            export_type=export_type,
            date_from_bj=date_from_bj,
            date_to_bj=date_to_bj,
            content=content,
            filename=filename,
        )
        return {
            'ok': True,
            'guild_name': normalized_guild,
            'period': normalized_period,
            'export_type': export_type,
            'date_from_bj': date_from_bj,
            'date_to_bj': date_to_bj,
            **stored,
        }

    @staticmethod
    def _timo_parse_revenue_date_bj(value: str) -> datetime.date:
        try:
            return datetime.strptime(str(value or '').strip(), '%Y-%m-%d').date()
        except Exception as exc:
            raise HTTPException(status_code=400, detail='invalid_timo_revenue_date') from exc

    @staticmethod
    def _timo_revenue_latest_complete_day_bj(now: Optional[datetime] = None) -> datetime.date:
        local_tz = ZoneInfo('Asia/Shanghai')
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current_bj = current.astimezone(local_tz)
        if current_bj.hour >= 16:
            return current_bj.date() - timedelta(days=1)
        return current_bj.date() - timedelta(days=2)

    def _timo_resolve_latest_complete_date_bj(self, value: Optional[str]) -> datetime.date:
        latest_day = self._timo_revenue_latest_complete_day_bj()
        if not str(value or '').strip():
            return latest_day
        target = self._timo_parse_revenue_date_bj(str(value or '').strip())
        if target > latest_day:
            raise HTTPException(status_code=400, detail=f'timo_data_date_not_ready:{latest_day.isoformat()}')
        return target

    def _timo_revenue_custom_range_bj(
        self,
        *,
        export_type: str,
        selected_date: str,
        now: Optional[datetime] = None,
    ) -> Tuple[str, str]:
        target = self._timo_parse_revenue_date_bj(selected_date)
        latest_day = self._timo_revenue_latest_complete_day_bj(now)
        normalized_type = str(export_type or '').strip().lower()
        if normalized_type == 'day':
            if target > latest_day:
                raise HTTPException(status_code=400, detail=f'timo_revenue_date_not_ready:{latest_day.isoformat()}')
            return target.isoformat(), target.isoformat()
        if normalized_type == 'week':
            start = target - timedelta(days=target.weekday())
            end = start + timedelta(days=6)
            if end > latest_day:
                raise HTTPException(status_code=400, detail=f'timo_revenue_week_not_ready:{latest_day.isoformat()}')
            return start.isoformat(), end.isoformat()
        raise HTTPException(status_code=400, detail='unsupported_timo_revenue_export_type')

    @staticmethod
    def _timo_local_day_to_export_marker(local_day: str) -> str:
        return datetime.strptime(str(local_day or '').strip(), '%Y-%m-%d').strftime('%Y%m%d')

    def _timo_guild_revenue_export_url(
        self,
        *,
        executor: Dict[str, Any],
        date_from_bj: str,
        date_to_bj: str,
        time_type: Any = '',
        max_attempts: int = 30,
        interval_seconds: float = 1.0,
    ) -> str:
        guild_id = str(executor.get('cms_guild_id') or executor.get('cms_guild_sid') or '').strip()
        guild_uuid = str(executor.get('cms_guild_sid') or executor.get('cms_guild_id') or '').strip()
        if not guild_id:
            raise HTTPException(status_code=400, detail='timo_guild_lock_required')
        payload = {
            'guildId': int(guild_id) if guild_id.isdigit() else guild_id,
            'uuid': guild_uuid,
            'timeType': time_type,
            'startTime': '' if str(time_type).strip() else self._timo_local_day_to_export_marker(date_from_bj),
            'endTime': '' if str(time_type).strip() else self._timo_local_day_to_export_marker(date_to_bj),
        }
        try:
            self._timo_guild_api_post(
                executor=executor,
                path='website-frontend/v1/officalWebGuild/exportGuildHostExcel',
                payload=payload,
                timeout_seconds=float(executor.get('request_timeout_seconds') or 30),
            )
        except Exception as exc:
            last_error = str(exc)
            if 'timo_ticket_expired' in last_error or 'ticket' in last_error.lower():
                raise HTTPException(status_code=401, detail='timo_ticket_expired')
            raise HTTPException(status_code=502, detail=f'timo_revenue_export_trigger_failed:{last_error[:160]}')
        last_error = ''
        for attempt in range(1, max(1, int(max_attempts or 1)) + 1):
            try:
                response = self._timo_guild_api_post(
                    executor=executor,
                    path='website-frontend/v1/officalWebGuild/getGuildHostExportUrl',
                    payload=payload,
                    timeout_seconds=float(executor.get('request_timeout_seconds') or 30),
                )
            except Exception as exc:
                last_error = str(exc)
                if 'timo_ticket_expired' in last_error or 'ticket' in last_error.lower():
                    raise HTTPException(status_code=401, detail='timo_ticket_expired')
                raise HTTPException(status_code=502, detail=f'timo_revenue_export_request_failed:{last_error[:160]}')
            export_url = str(response.get('data') or '').strip()
            if export_url:
                parsed = urlparse(export_url)
                if parsed.scheme not in {'http', 'https'}:
                    raise HTTPException(status_code=502, detail='timo_revenue_export_invalid_url')
                return export_url
            last_error = str(response.get('msg') or response.get('message') or f'export_url_not_ready_attempt_{attempt}')
            if attempt < max_attempts:
                time.sleep(max(0.1, float(interval_seconds or 0.1)))
        raise HTTPException(status_code=504, detail=f'timo_revenue_export_not_ready:{last_error[:160]}')

    def _fetch_timo_revenue_summary_override(
        self,
        *,
        executor: Dict[str, Any],
        time_type: Any,
        date_from_bj: str = '',
        date_to_bj: str = '',
    ) -> Dict[str, Any]:
        payload = {
            'uuid': str(executor.get('cms_guild_sid') or executor.get('cms_guild_id') or '').strip(),
            'timeType': time_type,
            'startTime': '' if str(time_type).strip() else self._timo_local_day_to_export_marker(date_from_bj),
            'endTime': '' if str(time_type).strip() else self._timo_local_day_to_export_marker(date_to_bj),
        }
        try:
            body = self._timo_guild_api_post(
                executor=executor,
                path='website-frontend/v1/officalWebGuild/getFrontPageStatByTimeRange',
                payload=payload,
                timeout_seconds=float(executor.get('request_timeout_seconds') or 30),
            )
        except Exception:
            return {}
        data = body.get('data') if isinstance(body.get('data'), dict) else body
        if not isinstance(data, dict):
            return {}
        mapping = {
            'active_1v1_hosts': 'activeFemaleNum',
            'quality_hosts': 'commandoFemaleTotal',
            'total_income': 'oneToOneIncomeTotal',
            'qualified_revenue': 'oneToOneStandardIncomeByLeaderInvited',
            'private_message': 'privateMsgIncome',
            'private_gift': 'privateGiftIncome',
            'call': 'oneToOneCallIncome',
            'matching': 'matchIncome',
            'quality_revenue': 'commandoFemaleSceneIncome',
        }
        override: Dict[str, Any] = {}
        for target, source in mapping.items():
            if data.get(source) in (None, ''):
                continue
            if target in {'active_1v1_hosts', 'quality_hosts'}:
                try:
                    override[target] = max(0, int(float(str(data.get(source)).replace(',', ''))))
                except Exception:
                    continue
            else:
                override[target] = self._timo_revenue_number(data.get(source))
        return override

    def export_timo_guild_revenue_xlsx(
        self,
        *,
        guild_name: str,
        user: Optional[Dict[str, Any]],
        period: str = 'yesterday',
        export_type: Optional[str] = None,
        date_bj: Optional[str] = None,
        use_cache: bool = True,
        join_time_by_timo_id: Optional[Dict[str, str]] = None,
    ) -> Tuple[bytes, str]:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name_required')
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        executor = self.resolve_timo_guild_executor(normalized_guild)
        if not executor or not executor.get('enabled'):
            raise HTTPException(status_code=404, detail='timo_guild_executor_not_found')
        if not str(executor.get('platform_authorization') or '').strip():
            raise HTTPException(status_code=400, detail='timo_ticket_not_configured')
        if str(date_bj or '').strip():
            normalized_export_type = str(export_type or 'day').strip().lower()
            date_from_bj, date_to_bj = self._timo_revenue_custom_range_bj(
                export_type=normalized_export_type,
                selected_date=str(date_bj or '').strip(),
            )
            if normalized_export_type not in {'day', 'week'}:
                raise HTTPException(status_code=400, detail='unsupported_timo_revenue_export_type')
            filename_period = normalized_export_type
            # Explicit dates must remain absolute. Timo's relative presets follow
            # calendar boundaries and can point at a newer, unsettled business day.
            timo_time_type: Any = ''
        else:
            normalized_period = str(period or 'yesterday').strip().lower()
            date_from_bj, date_to_bj = self._timo_revenue_export_range_bj(normalized_period)
            filename_period = normalized_period
            timo_time_type = {'yesterday': 1, 'last_week': 3}.get(normalized_period, '')
            normalized_export_type = 'week' if normalized_period == 'last_week' else 'day'
        if use_cache:
            cached = self._load_timo_revenue_export_cache(
                executor=executor,
                export_type=normalized_export_type,
                date_from_bj=date_from_bj,
                date_to_bj=date_to_bj,
            )
            if cached:
                return cached
            if not str(date_bj or '').strip():
                raise HTTPException(status_code=404, detail=f'timo_revenue_cache_not_ready:{date_from_bj}..{date_to_bj}')
        proxy_url = self._resolve_executor_proxy_url(executor)
        proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
        summary_override = self._fetch_timo_revenue_summary_override(
            executor=executor,
            time_type=timo_time_type,
            date_from_bj=date_from_bj,
            date_to_bj=date_to_bj,
        )
        last_mismatch_detail = ''
        for attempt in range(1, 4):
            export_url = self._timo_guild_revenue_export_url(
                executor=executor,
                date_from_bj=date_from_bj,
                date_to_bj=date_to_bj,
                time_type=timo_time_type,
            )
            try:
                response = requests.get(export_url, stream=True, proxies=proxies, timeout=90)
                response.raise_for_status()
                content = response.content
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f'timo_revenue_export_download_failed:{str(exc)[:160]}') from exc
            if not content:
                raise HTTPException(status_code=502, detail='timo_revenue_export_empty_file')
            self._store_timo_account_registration_times(
                executor_key=self._guild_anchor_executor_key(executor),
                registration_time_by_timo_id=self._timo_account_registration_times_from_revenue_content(content),
            )
            if join_time_by_timo_id is None:
                join_time_by_timo_id = self._fetch_timo_join_times_for_executor(executor)
            try:
                content = self._add_timo_revenue_summary_sheet(
                    content,
                    date_from_bj=date_from_bj,
                    date_to_bj=date_to_bj,
                    country=str(executor.get('country') or '').strip(),
                    join_time_by_timo_id=join_time_by_timo_id,
                    summary_override=summary_override,
                )
                break
            except HTTPException as exc:
                detail_text = str(exc.detail or '')
                if detail_text.startswith(('timo_join_time_missing_for_revenue_rows', 'timo_join_time_snapshot_missing')):
                    join_time_by_timo_id = self._fetch_timo_join_times_for_executor(executor)
                    content = self._add_timo_revenue_summary_sheet(
                        content,
                        date_from_bj=date_from_bj,
                        date_to_bj=date_to_bj,
                        country=str(executor.get('country') or '').strip(),
                        join_time_by_timo_id=join_time_by_timo_id,
                        summary_override=summary_override,
                    )
                    break
                if not detail_text.startswith('timo_revenue_export_detail_mismatch'):
                    raise
                last_mismatch_detail = detail_text
                if attempt >= 3:
                    raise HTTPException(status_code=502, detail=f'timo_revenue_export_detail_mismatch_after_retry:{last_mismatch_detail[:300]}') from exc
                time.sleep(1.0 * attempt)
        filename = self.timo_guild_revenue_export_filename(
            guild_name=normalized_guild,
            period=filename_period,
            date_from_bj=date_from_bj,
            date_to_bj=date_to_bj,
        )
        if use_cache and str(date_bj or '').strip():
            self._store_timo_revenue_export_cache(
                executor=executor,
                export_type=normalized_export_type,
                date_from_bj=date_from_bj,
                date_to_bj=date_to_bj,
                content=content,
                filename=filename,
            )
        return content, filename

    @staticmethod
    def _timo_revenue_number(value: Any) -> float:
        if value is None:
            return 0.0
        text = str(value).strip().replace(',', '').replace('%', '')
        if not text:
            return 0.0
        try:
            return float(text)
        except Exception:
            return 0.0

    @staticmethod
    def _timo_revenue_format(value: float) -> Any:
        rounded = round(float(value or 0.0), 1)
        if rounded.is_integer():
            return int(rounded)
        return rounded

    def _timo_revenue_detail_totals(self, detail_sheet: Any) -> Dict[str, Any]:
        header_row = next(detail_sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
        header_map = {str(value or '').strip().lower(): index for index, value in enumerate(header_row, start=1) if str(value or '').strip()}

        def col(*names: str) -> Optional[int]:
            for name in names:
                found = header_map.get(str(name or '').strip().lower())
                if found:
                    return found
            return None

        cols = {
            'total_income': col('1v1 total income', '1v1總收益', '1v1总收益'),
            'qualified_revenue': col('1v1 host qualified revenue this week', '本週1v1主播達標收益', '本周1v1主播达标收益'),
            'matching': col('Matching call earnings', '匹配通話收益', '匹配通话收益'),
            'private_message': col('Private message earnings', '私信消息收益'),
            'private_gift': col('Private gift earnings', '私信禮物收益', '私信礼物收益'),
            'call': col('1v1 call earnings', '1v1通話收益', '1v1通话收益'),
            'quality_host': col('Quality Host', '優質主播', '优质主播'),
            'quality_revenue': col('Specific Revenue for Quality Host', '優質主播特定場景收益', '优质主播特定场景收益'),
        }
        totals: Dict[str, Any] = {
            'positive_income_1v1_hosts': 0,
            'positive_income_quality_hosts': 0,
            'total_income': 0.0,
            'qualified_revenue': 0.0,
            'matching': 0.0,
            'private_message': 0.0,
            'private_gift': 0.0,
            'call': 0.0,
            'quality_revenue': 0.0,
        }
        for row_index in range(2, detail_sheet.max_row + 1):
            total_income = self._timo_revenue_number(detail_sheet.cell(row=row_index, column=cols['total_income']).value) if cols.get('total_income') else 0.0
            if total_income > 0:
                totals['positive_income_1v1_hosts'] += 1
            totals['total_income'] += total_income
            for key in ('qualified_revenue', 'matching', 'private_message', 'private_gift', 'call', 'quality_revenue'):
                if cols.get(key):
                    totals[key] += self._timo_revenue_number(detail_sheet.cell(row=row_index, column=cols[key]).value)
            if cols.get('quality_host'):
                quality_text = str(detail_sheet.cell(row=row_index, column=cols['quality_host']).value or '').strip().lower()
                if quality_text in {'yes', 'y', '是', 'true', '1'}:
                    totals['positive_income_quality_hosts'] += 1
        return totals

    def _timo_revenue_summary_detail_mismatches(self, detail_totals: Dict[str, Any], summary_values: Dict[str, Any]) -> List[str]:
        compare_keys = ('total_income', 'qualified_revenue', 'private_message', 'private_gift', 'call', 'matching')
        mismatches: List[str] = []
        for key in compare_keys:
            if key not in summary_values:
                continue
            detail_value = self._timo_revenue_number(detail_totals.get(key))
            summary_value = self._timo_revenue_number(summary_values.get(key))
            if abs(detail_value - summary_value) > 1.0:
                mismatches.append(f'{key}:detail={self._timo_revenue_format(detail_value)},summary={self._timo_revenue_format(summary_value)}')
        return mismatches

    def _timo_revenue_cached_export_mismatches(self, content: bytes) -> List[str]:
        try:
            workbook = load_workbook(io.BytesIO(content), data_only=True)
        except Exception as exc:
            return [f'invalid_xlsx:{str(exc)[:80]}']
        if '收益统计' not in workbook.sheetnames:
            return []
        detail_sheet = None
        for sheet_name in workbook.sheetnames:
            if sheet_name != '收益统计':
                detail_sheet = workbook[sheet_name]
                break
        if detail_sheet is None:
            return ['detail_sheet_missing']
        label_to_key = {
            '1v1 总收益': 'total_income',
            '1v1 主播达标收益': 'qualified_revenue',
            '私信收益': 'private_message',
            '私信礼物收益': 'private_gift',
            '通话收益': 'call',
            '匹配收益': 'matching',
        }
        summary_values: Dict[str, Any] = {}
        summary_sheet = workbook['收益统计']
        for row in summary_sheet.iter_rows(min_row=1, max_col=2, values_only=True):
            label = str((row[0] if row else '') or '').strip()
            key = label_to_key.get(label)
            if key:
                summary_values[key] = row[1] if len(row) > 1 else None
        if not summary_values:
            return []
        detail_totals = self._timo_revenue_detail_totals(detail_sheet)
        return self._timo_revenue_summary_detail_mismatches(detail_totals, summary_values)

    @staticmethod
    def _timo_revenue_summary_date_label(date_from_bj: str, date_to_bj: str) -> str:
        def format_day(value: str) -> str:
            try:
                parsed = datetime.strptime(str(value or '').strip(), '%Y-%m-%d')
                return f'{parsed.month} 月 {parsed.day} 日'
            except Exception:
                return str(value or '').strip()

        start = format_day(date_from_bj)
        end = format_day(date_to_bj)
        if str(date_from_bj or '').strip() == str(date_to_bj or '').strip():
            return f'{start}已统计并导出。'
        return f'{start}至 {end}已统计并导出。'

    @staticmethod
    def _timo_revenue_business_timezone(country: str) -> str:
        normalized = str(country or '').strip().lower()
        return {
            'brazil': 'America/Sao_Paulo',
            'br': 'America/Sao_Paulo',
            'mexico': 'America/Mexico_City',
            'mx': 'America/Mexico_City',
            'indonesia': 'Asia/Jakarta',
            'id': 'Asia/Jakarta',
        }.get(normalized, '')

    def _prune_timo_revenue_rows_after_business_period(
        self,
        detail_sheet: Any,
        *,
        date_to_bj: str,
        country: str,
    ) -> int:
        business_timezone = self._timo_revenue_business_timezone(country)
        if not business_timezone:
            return 0
        try:
            period_end = self._timo_parse_revenue_date_bj(date_to_bj)
            cutoff_business = datetime.combine(
                period_end + timedelta(days=1),
                datetime.min.time(),
                tzinfo=ZoneInfo(business_timezone),
            )
            cutoff_backend = cutoff_business.astimezone(ZoneInfo('Asia/Shanghai'))
        except Exception:
            return 0
        header_map = self._timo_revenue_header_map(detail_sheet)
        registered_col = self._timo_revenue_col(
            header_map, '入会时间', '入會時間', '主播註冊時間', '主播注册时间', 'registration time'
        )
        if not registered_col:
            return 0
        income_cols = [
            self._timo_revenue_col(header_map, *names)
            for names in (
                ('1v1 total income', '1v1總收益', '1v1总收益'),
                ('1v1 host qualified revenue this week', '本週1v1主播達標收益', '本周1v1主播达标收益'),
                ('Matching call earnings', '匹配通話收益', '匹配通话收益'),
                ('Private message earnings', '私信消息收益'),
                ('Private gift earnings', '私信禮物收益', '私信礼物收益'),
                ('1v1 call earnings', '1v1通話收益', '1v1通话收益'),
                ('Specific Revenue for Quality Host', '優質主播特定場景收益', '优质主播特定场景收益'),
            )
        ]
        income_cols = [column for column in income_cols if column]
        rows_to_delete: List[int] = []
        for row_index in range(2, detail_sheet.max_row + 1):
            raw_registered = str(detail_sheet.cell(row=row_index, column=registered_col).value or '').strip()
            try:
                registered_backend = datetime.strptime(raw_registered[:19], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=ZoneInfo('Asia/Shanghai')
                )
            except Exception:
                continue
            if registered_backend < cutoff_backend:
                continue
            has_income = any(
                abs(self._timo_revenue_number(detail_sheet.cell(row=row_index, column=column).value)) > 1e-9
                for column in income_cols
            )
            if has_income:
                raise HTTPException(
                    status_code=502,
                    detail=f'timo_revenue_post_join_income:{raw_registered}:{country}',
                )
            rows_to_delete.append(row_index)
        for row_index in reversed(rows_to_delete):
            detail_sheet.delete_rows(row_index, 1)
        return len(rows_to_delete)

    def _prune_timo_revenue_rows_without_income(self, detail_sheet: Any) -> int:
        header_map = self._timo_revenue_header_map(detail_sheet)
        income_cols = [
            self._timo_revenue_col(header_map, *names)
            for names in (
                ('1v1 total income', '1v1總收益', '1v1总收益'),
                ('1v1 host qualified revenue this week', '本週1v1主播達標收益', '本周1v1主播达标收益'),
                ('Matching call earnings', '匹配通話收益', '匹配通话收益'),
                ('Private message earnings', '私信消息收益'),
                ('Private gift earnings', '私信禮物收益', '私信礼物收益'),
                ('1v1 call earnings', '1v1通話收益', '1v1通话收益'),
                ('Specific Revenue for Quality Host', '優質主播特定場景收益', '优质主播特定场景收益'),
            )
        ]
        income_cols = [column for column in income_cols if column]
        if not income_cols:
            raise HTTPException(status_code=502, detail='timo_revenue_income_columns_missing')
        source_max_row = detail_sheet.max_row
        write_row = 2
        for source_row in range(2, source_max_row + 1):
            has_income = any(
                abs(self._timo_revenue_number(detail_sheet.cell(row=source_row, column=column).value)) > 1e-9
                for column in income_cols
            )
            if not has_income:
                continue
            if write_row != source_row:
                for column in range(1, detail_sheet.max_column + 1):
                    source_cell = detail_sheet.cell(row=source_row, column=column)
                    target_cell = detail_sheet.cell(row=write_row, column=column)
                    target_cell.value = source_cell.value
                    if source_cell.has_style:
                        target_cell._style = copy.copy(source_cell._style)
                    if source_cell.number_format:
                        target_cell.number_format = source_cell.number_format
            write_row += 1
        removed = source_max_row - write_row + 1
        if removed > 0:
            detail_sheet.delete_rows(write_row, removed)
        return max(0, removed)

    def _add_timo_revenue_summary_sheet(
        self,
        content: bytes,
        *,
        date_from_bj: str,
        date_to_bj: str,
        country: str = '',
        join_time_by_timo_id: Optional[Dict[str, str]] = None,
        summary_override: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        try:
            workbook = load_workbook(io.BytesIO(content))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f'timo_revenue_export_invalid_xlsx:{str(exc)[:120]}') from exc
        if '收益统计' in workbook.sheetnames:
            del workbook['收益统计']
        detail_sheet = workbook[workbook.sheetnames[0]]
        self._prune_timo_revenue_rows_without_income(detail_sheet)
        if join_time_by_timo_id is not None:
            self._apply_timo_join_time_contract(
                detail_sheet,
                join_time_by_timo_id=join_time_by_timo_id,
            )
        self._prune_timo_revenue_rows_after_business_period(
            detail_sheet,
            date_to_bj=date_to_bj,
            country=country,
        )
        totals = self._timo_revenue_detail_totals(detail_sheet)
        mismatch_errors = self._timo_revenue_summary_detail_mismatches(totals, dict(summary_override or {}))
        if mismatch_errors:
            raise HTTPException(status_code=502, detail='timo_revenue_export_detail_mismatch:' + ';'.join(mismatch_errors[:3]))
        for key, value in dict(summary_override or {}).items():
            if key not in totals:
                continue
            totals[key] = self._timo_revenue_number(value)

        summary = workbook.create_sheet('收益统计', 0)
        summary.append([self._timo_revenue_summary_date_label(date_from_bj, date_to_bj)])
        summary.append([])
        summary.append(['收益统计', ''])
        rows = [
            ('有收益 1v1 主播数', totals['positive_income_1v1_hosts']),
            ('有收益优质主播数', totals['positive_income_quality_hosts']),
            ('1v1 总收益', self._timo_revenue_format(totals['total_income'])),
            ('1v1 主播达标收益', self._timo_revenue_format(totals['qualified_revenue'])),
            ('私信收益', self._timo_revenue_format(totals['private_message'])),
            ('私信礼物收益', self._timo_revenue_format(totals['private_gift'])),
            ('通话收益', self._timo_revenue_format(totals['call'])),
            ('匹配收益', self._timo_revenue_format(totals['matching'])),
            ('公会优质主播场景收益', self._timo_revenue_format(totals['quality_revenue'])),
        ]
        for label, value in rows:
            summary.append([label, value])
        header_fill = PatternFill(fill_type='solid', fgColor='DBEAFE')
        header_font = Font(bold=True, color='1E3A8A')
        summary['A1'].font = Font(bold=True, size=14, color='0F172A')
        summary['A3'].fill = header_fill
        summary['B3'].fill = header_fill
        summary['A3'].font = header_font
        summary['B3'].font = header_font
        summary.column_dimensions['A'].width = 28
        summary.column_dimensions['B'].width = 18
        for row in summary.iter_rows(min_row=4, max_row=summary.max_row, min_col=1, max_col=2):
            row[0].alignment = Alignment(horizontal='left')
            row[1].alignment = Alignment(horizontal='right')
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def timo_guild_revenue_export_filename(self, *, guild_name: str, period: str, date_from_bj: Optional[str] = None, date_to_bj: Optional[str] = None) -> str:
        display_guild = self._timo_intake_guild_display_name(str(guild_name or '').strip())
        safe_guild = re.sub(r'[^A-Za-z0-9]+', '', display_guild) or 'Timo'
        normalized_period = str(period or '').strip().lower()
        start, end = (date_from_bj, date_to_bj) if date_from_bj and date_to_bj else self._timo_revenue_export_range_bj(normalized_period)
        start_compact = re.sub(r'\D+', '', str(start or ''))[-6:]
        end_compact = re.sub(r'\D+', '', str(end or ''))[-6:]
        suffix = start_compact if start_compact == end_compact else f'{start_compact}-{end_compact}'
        if normalized_period in {'yesterday', 'day'}:
            label = 'RevenueDay'
        else:
            label = 'RevenueWeek'
        return f'{safe_guild}{label}{suffix}.xlsx'

    def clear_timo_intake_item_card(self, *, item_id: str, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        item = self._get_timo_intake_item(item_id)
        if not self._ops_timo_intake_user_can_access_item(user, item):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        if str(item.get('feedback_status') or '') == 'cleared':
            return {'ok': True, 'item': item}
        if str(item.get('system_status') or '') in {'crm_success', 'verified_success'} and str(item.get('feedback_status') or '') == 'pending_feedback':
            raise HTTPException(status_code=400, detail='successful_item_requires_feedback_done')
        if str(item.get('system_status') or '') in {'pending_verification', 'crm_pending'}:
            raise HTTPException(status_code=400, detail='pending_timo_item_cannot_clear')
        now = utc_now()
        done_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE ops_timo_intake_items SET feedback_status='cleared', feedback_done_at=COALESCE(feedback_done_at, ?), feedback_done_by=COALESCE(feedback_done_by, ?), updated_at=? WHERE item_id=?",
                (now, done_by, now, str(item_id or '').strip()),
            )
            conn.commit()
        return {'ok': True, 'item': self._get_timo_intake_item(item_id)}

    def clear_timo_intake_stale_feedback_items(self, *, guild_name: str, user: Optional[Dict[str, Any]], threshold_minutes: int = 120) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip()
        if not normalized_guild:
            raise HTTPException(status_code=400, detail='guild_name_required')
        if not self._ops_intake_user_can_access_guild(user, normalized_guild):
            raise HTTPException(status_code=403, detail='ops_guild_intake_forbidden')
        threshold = max(1, int(threshold_minutes or 120))
        cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=threshold)
        cleared_by = str((user or {}).get('display_name') or (user or {}).get('username') or (user or {}).get('user_id') or 'ops_user').strip()
        now = utc_now()
        cleared_ids: List[str] = []
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT item_id, system_status, feedback_status, timo_verified_at, updated_at, created_at
                FROM ops_timo_intake_items
                WHERE guild_name = ?
                  AND COALESCE(feedback_status, '') IN ('pending_feedback', 'not_feedbackable')
                """,
                (normalized_guild,),
            ).fetchall()
            for row in rows:
                item = dict(row)
                if str(item.get('system_status') or '').strip() in {'pending_verification', 'crm_pending'}:
                    continue
                age_source = str(item.get('timo_verified_at') or item.get('updated_at') or item.get('created_at') or '').strip()
                if not age_source:
                    continue
                try:
                    age_dt = parse_iso_datetime(age_source)
                except Exception:
                    continue
                if age_dt <= cutoff_dt:
                    item_id = str(item.get('item_id') or '').strip()
                    if item_id:
                        cleared_ids.append(item_id)
            if cleared_ids:
                placeholders = ','.join('?' for _ in cleared_ids)
                conn.execute(
                    f"UPDATE ops_timo_intake_items SET feedback_status='cleared', feedback_done_at=COALESCE(feedback_done_at, ?), feedback_done_by=COALESCE(feedback_done_by, ?), updated_at=? WHERE item_id IN ({placeholders})",
                    (now, cleared_by, now, *cleared_ids),
                )
                conn.commit()
        return {
            'ok': True,
            'guild_name': normalized_guild,
            'threshold_minutes': threshold,
            'cutoff_at': cutoff_dt.isoformat(),
            'cleared_count': len(cleared_ids),
            'cleared_item_ids': cleared_ids,
        }

    def verify_timo_intake_item(self, *, item_id: str, force_crm_sync: bool = False) -> Dict[str, Any]:
        normalized_item_id = str(item_id or '').strip()
        if not normalized_item_id:
            raise HTTPException(status_code=400, detail='item_id is required.')
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM ops_timo_intake_items WHERE item_id = ?", (normalized_item_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail='timo_intake_item_not_found')
            item = dict(row)
            now = utc_now()
            timo_id = self._normalize_timo_id(item.get('timo_id'))
            try:
                executor = self._build_timo_executor_for_item(item)
                timo_result = executor.verify_host_membership(timo_id=timo_id)
            except Exception as exc:  # noqa: BLE001
                timo_result = {
                    'ok': False,
                    'verified': False,
                    'result_code': 'timo_request_failed',
                    'result_reason': str(exc),
                    'request_payload': {
                        'timo_id': timo_id,
                        'guild_name': str(item.get('guild_name') or ''),
                    },
                }
            timo_verified = bool(timo_result.get('ok') and timo_result.get('verified') is True)
            if timo_verified:
                timo_status = 'success'
            elif str(timo_result.get('result_code') or '') == 'timo_member_not_found':
                timo_status = 'not_found'
            else:
                timo_status = 'failed'
            system_status = str(item.get('system_status') or 'crm_pending').strip() or 'crm_pending'
            crm_result = {
                'crm_sync_status': item.get('crm_sync_status') or 'not_started',
                'crm_result_code': item.get('crm_result_code') or '',
                'crm_result_reason': item.get('crm_result_reason') or '',
                'crm_payload': self._decode_json_field(item.get('crm_payload')),
                'crm_response': self._decode_json_field(item.get('crm_response')),
                'crm_synced_at': item.get('crm_synced_at'),
            }
            if timo_verified and (force_crm_sync or str(item.get('crm_sync_status') or '') != 'success'):
                crm_result = self._sync_timo_intake_crm(conn, item=item, timo_result=timo_result)
            crm_status = str(crm_result.get('crm_sync_status') or '').strip()
            if not timo_verified:
                system_status = 'verify_failed'
            elif crm_status == 'success':
                system_status = 'crm_success'
            elif crm_status in {'failed', 'skipped'}:
                # A successful Timo membership check must never remain in the
                # generic pending state when CRM cannot run. Keeping it pending
                # makes the UI falsely claim that membership verification is
                # still outstanding. Surface the CRM failure explicitly and
                # keep the item retryable through the normal sync action.
                system_status = 'crm_failed'
            elif system_status in {'pending_verification', 'verify_failed', 'verified_success'}:
                system_status = 'crm_pending'
            feedback_status = 'pending_feedback' if system_status == 'crm_success' else 'not_feedbackable'
            if str(item.get('feedback_status') or '') == 'feedback_done' and system_status == 'crm_success':
                feedback_status = 'feedback_done'
            conn.execute(
                """
                UPDATE ops_timo_intake_items
                SET system_status = ?, timo_verify_status = ?, timo_result_code = ?,
                    timo_result_reason = ?, timo_result_snapshot = ?, timo_verified_at = ?,
                    feedback_status = ?,
                    crm_sync_status = ?, crm_result_code = ?, crm_result_reason = ?,
                    crm_payload = ?, crm_response = ?, crm_synced_at = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (
                    system_status,
                    timo_status,
                    str(timo_result.get('result_code') or ''),
                    str(timo_result.get('result_reason') or ''),
                    json.dumps(timo_result, ensure_ascii=False),
                    now,
                    feedback_status,
                    str(crm_result.get('crm_sync_status') or 'not_started'),
                    str(crm_result.get('crm_result_code') or ''),
                    str(crm_result.get('crm_result_reason') or ''),
                    json.dumps(crm_result.get('crm_payload') or {}, ensure_ascii=False),
                    json.dumps(crm_result.get('crm_response') or {}, ensure_ascii=False),
                    crm_result.get('crm_synced_at'),
                    now,
                    normalized_item_id,
                ),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM ops_timo_intake_items WHERE item_id = ?", (normalized_item_id,)).fetchone()
        return {
            'ok': bool(timo_result.get('ok')) and (bool(timo_result.get('verified')) or str(crm_result.get('crm_sync_status') or '') == 'success'),
            'verified': True if timo_result.get('verified') is True else (False if timo_result.get('verified') is False else None),
            'crm_synced': str(crm_result.get('crm_sync_status') or '') == 'success',
            'item': self._public_timo_intake_row(dict(updated) if updated else item),
            'timo_result': timo_result,
            'crm': crm_result,
        }

    def replay_timo_ticket_expired_intake_items(
        self,
        *,
        guild_name: str,
        limit: int = 100,
    ) -> Dict[str, Any]:
        normalized_guild_name = str(guild_name or '').strip()
        if not normalized_guild_name:
            return {'ok': False, 'guild_name': '', 'error': 'missing_guild_name'}
        safe_limit = max(1, min(500, int(limit or 100)))
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with self.db.connect() as conn:
            candidates = [dict(row) for row in conn.execute(
                """
                SELECT item_id
                FROM ops_timo_intake_items
                WHERE guild_name = ?
                  AND system_status = 'verify_failed'
                  AND timo_result_code = 'timo_ticket_expired'
                  AND COALESCE(feedback_status, '') != 'cleared'
                  AND (
                    COALESCE(timo_verify_status, '') != 'retrying_ticket_recovery'
                    OR datetime(updated_at) <= datetime(?)
                  )
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (normalized_guild_name, stale_before, safe_limit),
            ).fetchall()]
        summary = {
            'ok': True,
            'guild_name': normalized_guild_name,
            'candidate_count': len(candidates),
            'claimed_count': 0,
            'verified_count': 0,
            'not_found_count': 0,
            'ticket_expired_count': 0,
            'failed_count': 0,
            'processed_item_ids': [],
        }
        for candidate in candidates:
            item_id = str(candidate.get('item_id') or '').strip()
            if not item_id:
                continue
            claimed_at = utc_now()
            with self.db.connect() as conn:
                claim = conn.execute(
                    """
                    UPDATE ops_timo_intake_items
                    SET timo_verify_status = 'retrying_ticket_recovery', updated_at = ?
                    WHERE item_id = ?
                      AND guild_name = ?
                      AND system_status = 'verify_failed'
                      AND timo_result_code = 'timo_ticket_expired'
                      AND COALESCE(feedback_status, '') != 'cleared'
                      AND (
                        COALESCE(timo_verify_status, '') != 'retrying_ticket_recovery'
                        OR datetime(updated_at) <= datetime(?)
                      )
                    """,
                    (claimed_at, item_id, normalized_guild_name, stale_before),
                )
                conn.commit()
            if int(claim.rowcount or 0) != 1:
                continue
            summary['claimed_count'] += 1
            summary['processed_item_ids'].append(item_id)
            try:
                replay = self.verify_timo_intake_item(item_id=item_id, force_crm_sync=True)
                timo_result = dict(replay.get('timo_result') or {})
                result_code = str(timo_result.get('result_code') or '').strip()
                if replay.get('verified') is True:
                    summary['verified_count'] += 1
                elif result_code == 'timo_member_not_found':
                    summary['not_found_count'] += 1
                elif result_code == 'timo_ticket_expired':
                    summary['ticket_expired_count'] += 1
                else:
                    summary['failed_count'] += 1
            except Exception as exc:  # noqa: BLE001
                summary['failed_count'] += 1
                with self.db.connect() as conn:
                    conn.execute(
                        """
                        UPDATE ops_timo_intake_items
                        SET timo_verify_status = 'failed',
                            timo_result_reason = ?,
                            updated_at = ?
                        WHERE item_id = ?
                          AND timo_verify_status = 'retrying_ticket_recovery'
                        """,
                        (f'ticket_recovery_replay_failed:{str(exc)[:240]}', utc_now(), item_id),
                    )
                    conn.commit()
        summary['ok'] = summary['ticket_expired_count'] == 0 and summary['failed_count'] == 0
        return summary

    def _build_timo_executor_for_item(self, item: Dict[str, Any]) -> Any:
        if self.timo_guild_executor is not None:
            return self.timo_guild_executor
        executor = self.resolve_timo_guild_executor(str((item or {}).get('guild_name') or '').strip()) or self._find_fallback_timo_guild_executor_config() or {}
        base_url = str(executor.get('platform_backend_url') or '').strip() or str(os.getenv('TIMO_API_BASE_URL') or '').strip() or TIMO_DEFAULT_API_BASE_URL
        ticket = str(executor.get('platform_authorization') or '').strip() or str(os.getenv('TIMO_TICKET') or os.getenv('TIMO_PLATFORM_AUTHORIZATION') or '').strip()
        guild_uuid = (
            str(executor.get('cms_guild_sid') or executor.get('cms_guild_id') or '').strip()
            or str(os.getenv('TIMO_USER_UUID') or os.getenv('TIMO_GUILD_UUID') or '').strip()
        )
        timeout_seconds = executor.get('request_timeout_seconds') or os.getenv('TIMO_REQUEST_TIMEOUT_SECONDS') or 15
        return TimoGuildExecutor(
            base_url=base_url,
            ticket=ticket,
            lang=str(os.getenv('TIMO_LANG') or 'zh_TW').strip() or 'zh_TW',
            guild_uuid=guild_uuid,
            timeout_seconds=float(timeout_seconds or 15),
        )

    def _find_fallback_timo_guild_executor_config(self) -> Dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT guild_name, COALESCE(app_name, 'linky') AS app_name, backend_url, login_username, password_secret_ref, guild_backend_token,
                       oauth_token, oauth_token_secret, platform_backend_url, platform_authorization,
                       cms_guild_id, cms_guild_sid, country, proxy_url, proxy_region, proxy_type,
                       enabled, browser_profile_key, bind_concurrency, request_timeout_seconds, notes, updated_at
                FROM guild_executors
                WHERE enabled = 1
                  AND (
                    LOWER(COALESCE(app_name, 'linky')) = 'timo'
                    OR
                    LOWER(COALESCE(platform_backend_url, '')) LIKE '%touchchat%'
                    OR LOWER(COALESCE(platform_backend_url, '')) LIKE '%timo%'
                    OR LOWER(COALESCE(notes, '')) LIKE '%timo%'
                    OR LOWER(guild_name) LIKE '%timo%'
                  )
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _decode_json_field(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value or '{}'))
        except Exception:
            return {}

    def _sync_timo_intake_crm(self, conn: sqlite3.Connection, *, item: Dict[str, Any], timo_result: Dict[str, Any]) -> Dict[str, Any]:
        if self.crm_adapter is None:
            return {
                'crm_sync_status': 'skipped',
                'crm_result_code': 'crm_not_configured',
                'crm_result_reason': 'CRM adapter is not configured.',
                'crm_payload': {},
                'crm_response': {},
                'crm_synced_at': None,
            }
        member = timo_result.get('member') if isinstance(timo_result.get('member'), dict) else {}
        app_name = str(item.get('app_name') or os.getenv('TIMO_CRM_APP_NAME') or 'Timo').strip() or 'Timo'
        resolved_app = self._resolve_crm_app_mapping(app_name)
        guild_identity = resolve_timo_guild_identity(item.get('guild_name'))
        dept_name = (
            guild_identity.display_name if guild_identity else ''
        ) or (
            str(os.getenv('TIMO_CRM_DEPT_NAME') or '').strip()
            or self._timo_intake_guild_display_name(str(item.get('guild_name') or '').strip())
        )
        resolved_dept = self._resolve_crm_dept_mapping(
            dept_name,
            guild_identity.crm_dept_id if guild_identity else None,
        )
        if dept_name and not str(resolved_dept.get('deptId') or '').strip():
            from app.timo_guild_identity import timo_guild_storage_name

            storage_name = timo_guild_storage_name(dept_name)
            if storage_name and storage_name != dept_name:
                legacy_dept = self._resolve_crm_dept_mapping(storage_name)
                if str(legacy_dept.get('deptId') or '').strip():
                    resolved_dept = {
                        **legacy_dept,
                        'deptName': dept_name,
                        'mapping_source': 'stable_guild_identity_alias',
                    }
        timo_id = self._normalize_timo_id(item.get('timo_id'))
        normalized_mobile = self._normalize_timo_mobile(item.get('mobile'))
        id_only_mobile_placeholder = '' if normalized_mobile else self._make_timo_id_only_phone(timo_id)
        mobile = normalized_mobile or None
        phone_raw = normalized_mobile or id_only_mobile_placeholder
        crm_payload = {
            'mobile': mobile,
            'mobilePlaceholder': id_only_mobile_placeholder,
            'phoneRaw': phone_raw,
            'phoneE164': normalized_mobile if normalized_mobile.startswith('+') else '',
            'ywId': timo_id,
            'name': str(member.get('nickName') or ''),
            'remark': str(timo_result.get('result_reason') or ''),
            'dept': '',
            'wa': '',
            'areaCode': '',
            'inviterId': str(member.get('inviterUserId') or ''),
            'appName': resolved_app['appName'],
            'appId': resolved_app['appId'],
            'pendaftaranGroup': str(item.get('group_name') or ''),
            'paymentStatus': '',
            'pzStatus': 0,
            'userQuality': '',
            'fileUrl': '',
            'deptName': resolved_dept['deptName'],
            'deptId': resolved_dept['deptId'],
            'submissionId': str(item.get('item_id') or ''),
            'sourceChannel': str(item.get('source_channel') or 'ops_timo_intake'),
            'sourceApp': app_name,
            'guildName': self._timo_intake_guild_display_name(str(item.get('guild_name') or '')),
            'executorGuildName': self._timo_intake_guild_display_name(str(item.get('guild_name') or '')),
            'creatorName': str(item.get('submitted_by_username') or ''),
            'bindStatus': 'timo_guild_verified',
            'officialGroupStatus': 'not_applicable',
            'timoMember': member,
        }
        mapping_failure = self._precheck_crm_mapping_failure(resolved_app=resolved_app, resolved_dept=resolved_dept)
        if mapping_failure:
            self._record_sync_log(
                conn,
                lead_id=None,
                task_id=str(item.get('item_id') or ''),
                sync_type='timo_customer_upsert',
                target_system='crm',
                status='failed',
                request_snapshot=crm_payload,
                response_snapshot={'mapping_failure': mapping_failure, 'resolved_app': resolved_app, 'resolved_dept': resolved_dept},
            )
            return {
                'crm_sync_status': 'failed',
                'crm_result_code': 'crm_mapping_failed',
                'crm_result_reason': mapping_failure,
                'crm_payload': crm_payload,
                'crm_response': {'mapping_failure': mapping_failure},
                'crm_synced_at': None,
            }
        try:
            crm_response = self.crm_adapter.create_customer(crm_payload)
        except Exception as exc:
            crm_response = {'code': -1, 'msg': str(exc)}
        verified_row = None
        # A duplicate CRM SID proves that an older record exists, but it does not
        # prove that this submission was written. Keep the current intake failed
        # so operators can distinguish membership verification from CRM writeback.
        crm_write_confirmed = self._crm_response_confirms_customer_write(crm_response, allow_duplicate_sid=False)
        if isinstance(crm_response, dict) and (crm_response.get('code') == 0 or crm_write_confirmed):
            verified_row = self._find_existing_customer_with_fallback(
                yw_id=timo_id,
                mobile=mobile,
                crm_response=crm_response,
                app_name=resolved_app['appName'],
                dept_name=resolved_dept['deptName'],
                registration_group=str(item.get('group_name') or ''),
                allow_empty_mobile_match=bool(id_only_mobile_placeholder),
            )
            if not verified_row and crm_write_confirmed:
                verified_row = {
                    'id': (crm_response.get('data') or {}).get('customerId'),
                    'ywId': timo_id,
                    'mobile': mobile,
                    'mobilePlaceholder': id_only_mobile_placeholder,
                    'appName': resolved_app['appName'],
                    'deptName': resolved_dept['deptName'],
                    'pendaftaranGroup': str(item.get('group_name') or ''),
                    '_source': 'automation_upsert_response',
                }
        crm_code_ok = bool(isinstance(crm_response, dict) and crm_response.get('code') == 0)
        success = bool(isinstance(crm_response, dict) and (crm_write_confirmed or crm_code_ok) and verified_row)
        status = 'success' if success else 'failed'
        reason = '' if success else self._normalize_crm_failure_reason(crm_response if isinstance(crm_response, dict) else {}, fallback_found=False)
        response_snapshot = {'crm_response': crm_response, 'verified_after_write': bool(verified_row), 'verified_row': verified_row or {}}
        self._record_sync_log(
            conn,
            lead_id=None,
            task_id=str(item.get('item_id') or ''),
            sync_type='timo_customer_upsert',
            target_system='crm',
            status=status,
            request_snapshot=crm_payload,
            response_snapshot=response_snapshot,
        )
        return {
            'crm_sync_status': status,
            'crm_result_code': 'crm_synced' if success else 'crm_sync_failed',
            'crm_result_reason': reason,
            'crm_payload': crm_payload,
            'crm_response': response_snapshot,
            'crm_synced_at': utc_now() if success else None,
        }

    def _public_timo_intake_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        public = dict(row or {})
        public.setdefault('guild_name', '')
        guild_contract = timo_guild_contract_fields(public.get('guild_name'))
        public['guild_id'] = guild_contract.get('guild_id', '')
        public['guild_sid'] = guild_contract.get('guild_sid', '')
        public['guild_display_name'] = (
            guild_contract.get('guild_display_name')
            or self._timo_intake_guild_display_name(str(public.get('guild_name') or ''))
        )
        public['timo_result_snapshot'] = self._decode_json_field(public.get('timo_result_snapshot'))
        public['crm_payload'] = self._decode_json_field(public.get('crm_payload'))
        public['crm_response'] = self._decode_json_field(public.get('crm_response'))
        public['timo_result_reason_display'] = self._humanize_timo_failure_reason(
            public.get('timo_result_reason'),
            public.get('timo_result_code'),
        )
        public['crm_result_reason_display'] = self._humanize_timo_failure_reason(
            public.get('crm_result_reason'),
            public.get('crm_result_code'),
        )
        return public

    def list_guild_executors(self, *, app_name: str = 'linky') -> Dict[str, Any]:
        normalized_app = str(app_name or 'linky').strip().lower() or 'linky'
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """
                SELECT ge.guild_name, COALESCE(ge.app_name, 'linky') AS app_name, ge.backend_url, ge.login_username, ge.platform_backend_url, ge.cms_guild_id, ge.cms_guild_sid, ge.country, ge.guild_country, ge.eligible_user_countries, ge.routing_region, ge.proxy_url, ge.proxy_region, ge.proxy_type, ge.enabled, ge.browser_profile_key, ge.bind_concurrency, ge.request_timeout_seconds, ge.notes, ge.updated_at,
                       CASE WHEN COALESCE(ge.password_secret_ref, '') != '' THEN 1 ELSE 0 END AS password_configured,
                       CASE WHEN COALESCE(ge.oauth_token, '') != '' AND COALESCE(ge.oauth_token_secret, '') != '' THEN 1 ELSE 0 END AS oauth_configured,
                       CASE WHEN COALESCE(ge.guild_backend_token, '') != '' THEN 1 ELSE 0 END AS guild_backend_token_configured,
                       CASE WHEN COALESCE(ge.platform_authorization, '') != '' THEN 1 ELSE 0 END AS platform_authorization_configured,
                       CASE WHEN COALESCE(ct.refresh_token, '') != '' THEN 1 ELSE 0 END AS cms_refresh_token_configured
                FROM guild_executors ge
                LEFT JOIN cms_executor_tokens ct ON ct.guild_name = ge.guild_name
                WHERE LOWER(COALESCE(ge.app_name, 'linky')) = ?
                ORDER BY ge.guild_name ASC
                """
                , (normalized_app,)
            ).fetchall()]
        for row in rows:
            row.update(guild_country_contract(row))
            row['country'] = row.get('guild_country') or row.get('country') or ''
            row['enabled'] = bool(row.get('enabled'))
            row['password_configured'] = bool(row.get('password_configured'))
            row['oauth_configured'] = bool(row.get('oauth_configured'))
            row['guild_backend_token_configured'] = bool(row.get('guild_backend_token_configured'))
            row['platform_authorization_configured'] = bool(row.get('platform_authorization_configured'))
            row['cms_refresh_token_configured'] = bool(row.get('cms_refresh_token_configured'))
            effective_proxy_url = self._resolve_executor_proxy_url(row)
            row['proxy_effective_configured'] = bool(effective_proxy_url)
            row['proxy_region_mapping_configured'] = bool(str(row.get('proxy_region') or '').strip() and self.guild_executor_proxy_region_urls.get(str(row.get('proxy_region') or '').strip()))
        return {
            'rows': rows,
            'proxy_region_options': GUILD_EXECUTOR_PROXY_REGION_OPTIONS,
        }

    @staticmethod
    def _timo_reward_number(value: Any) -> float:
        if isinstance(value, list):
            for item in value:
                number = Service._timo_reward_number(item)
                if number > 0:
                    return number
            return 0.0
        if value is None:
            return 0.0
        text = str(value).strip().replace(',', '')
        if not text:
            return 0.0
        if ':' in text:
            text = text.split(':', 1)[0].strip()
        if '/' in text:
            text = text.split('/', 1)[0].strip()
        try:
            return float(text)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _timo_reward_track_key(task: Dict[str, Any], index: int = 0) -> str:
        reward_type = str(task.get('reward_type') or task.get('claim_reward_type') or '').strip()
        if reward_type == 'guild_task_reward':
            return 'guild_task'
        if reward_type == 'quality_host_reward':
            return 'quality_host'
        name = str(task.get('task_name') or task.get('name') or task.get('title') or '').lower()
        main_task = str(task.get('main_task') or task.get('task_type') or '').strip()
        if main_task == '10001' or '优质' in name or '優質' in name or 'quality' in name:
            return 'quality_host'
        task_index = task.get('index')
        try:
            task_index_int = int(task_index)
        except (TypeError, ValueError):
            task_index_int = index
        if task_index_int == 1:
            return 'quality_host'
        return 'guild_task'

    @staticmethod
    def _timo_reward_task_target(task: Dict[str, Any]) -> float:
        for key in ('task_target', 'target', 'threshold', 'target_diamonds'):
            value = Service._timo_reward_number(task.get(key))
            if value > 0:
                return value
        process = str(task.get('process') or '').strip()
        if '/' in process:
            return Service._timo_reward_number(process.split('/', 1)[1])
        return 0.0

    @staticmethod
    def _timo_reward_task_progress(task: Dict[str, Any]) -> float:
        for key in ('task_progress', 'progress', 'current', 'current_diamonds'):
            value = Service._timo_reward_number(task.get(key))
            if value > 0:
                return value
        process = str(task.get('process') or '').strip()
        if '/' in process:
            return Service._timo_reward_number(process.split('/', 1)[0])
        return 0.0

    @staticmethod
    def _timo_reward_task_reward(task: Dict[str, Any]) -> Optional[int]:
        raw = task.get('reward')
        if isinstance(raw, list):
            raw = next((item for item in raw if Service._timo_reward_number(item) > 0), None)
        if raw is None:
            raw = task.get('rewards') or task.get('reward_diamonds') or task.get('claimed_reward')
        value = Service._timo_reward_number(raw)
        return int(value) if value > 0 else None

    @staticmethod
    def _timo_reward_epoch_seconds(value: Any) -> float:
        if value in (None, ''):
            return 0.0
        try:
            number = float(value)
            return number / 1000.0 if number > 100000000000 else number
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(str(value).strip().replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _timo_reward_task_cycle_end(task: Dict[str, Any]) -> float:
        for key in ('end_time', 'endTime', 'cycle_end_at'):
            value = Service._timo_reward_epoch_seconds(task.get(key))
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _load_timo_reward_claim_status() -> Dict[str, Any]:
        try:
            if not TIMO_REWARD_CLAIM_STATE_PATH.exists():
                return {}
            payload = json.loads(TIMO_REWARD_CLAIM_STATE_PATH.read_text(encoding='utf-8'))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _load_timo_reward_claim_history_rewards(guild_name: str) -> Dict[str, Dict[int, int]]:
        rewards: Dict[str, Dict[int, int]] = {'guild_task': {}, 'quality_host': {}}
        normalized_guild = str(guild_name or '').strip().lower()
        if not normalized_guild:
            return rewards
        try:
            history_index = _load_timo_reward_history_index()
            for entry in (history_index.get('entries_by_guild') or {}).get(normalized_guild, ()):
                result = entry.get('result') if isinstance(entry, dict) else None
                candidates: List[Dict[str, Any]] = []
                if not isinstance(result, dict):
                    continue
                claims = result.get('claims')
                if isinstance(claims, list):
                    for claim in claims:
                        if isinstance(claim, dict) and claim.get('ok') is True and isinstance(claim.get('task'), dict):
                            task = dict(claim['task'])
                            if claim.get('reward_type'):
                                task.setdefault('reward_type', claim.get('reward_type'))
                            candidates.append(task)
                for key in ('claimed_tasks', 'claimed_rewards'):
                    value = result.get(key)
                    if isinstance(value, list):
                        candidates.extend(item for item in value if isinstance(item, dict))
                for index, task in enumerate(candidates):
                    target = int(Service._timo_reward_task_target(task))
                    reward = Service._timo_reward_task_reward(task)
                    if target > 0 and reward:
                        rewards.setdefault(Service._timo_reward_track_key(task, index), {})[target] = reward
        except Exception:
            return rewards
        return rewards

    @staticmethod
    def _load_timo_reward_current_cycle_claims(
        guild_name: str,
        cycle_end_by_key: Dict[str, float],
        record_cycle_ends: Optional[Dict[Tuple[str, int], float]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        states: Dict[str, Dict[str, Any]] = {
            'guild_task': {'claims': [], 'segmented': False},
            'quality_host': {'claims': [], 'segmented': False},
        }
        normalized_guild = str(guild_name or '').strip().lower()
        normalized_cycle_ends = {
            key: Service._timo_reward_epoch_seconds(value)
            for key, value in cycle_end_by_key.items()
            if Service._timo_reward_epoch_seconds(value) > 0
        }
        normalized_record_cycle_ends = {
            (str(record_id or '').strip(), int(target or 0)): Service._timo_reward_epoch_seconds(value)
            for (record_id, target), value in (record_cycle_ends or {}).items()
            if str(record_id or '').strip()
            and int(target or 0) > 0
            and Service._timo_reward_epoch_seconds(value) > 0
        }
        if not normalized_guild or not normalized_cycle_ends:
            return states
        seen: Dict[str, set[str]] = {'guild_task': set(), 'quality_host': set()}
        try:
            history_index = _load_timo_reward_history_index()
            for entry in (history_index.get('entries_by_guild') or {}).get(normalized_guild, ()):
                if not isinstance(entry, dict):
                    continue
                result = entry.get('result')
                if not isinstance(result, dict):
                    continue
                payload_checked_at = entry.get('payload_checked_at')
                for index, claim in enumerate(result.get('claims') or []):
                    if not isinstance(claim, dict) or claim.get('ok') is not True or not isinstance(claim.get('task'), dict):
                        continue
                    task = dict(claim['task'])
                    if claim.get('reward_type'):
                        task.setdefault('reward_type', claim.get('reward_type'))
                    key = Service._timo_reward_track_key(task, index)
                    cycle_end = normalized_cycle_ends.get(key, 0.0)
                    target = int(Service._timo_reward_task_target(task))
                    reward = Service._timo_reward_task_reward(task)
                    record_id = str(task.get('record_id') or task.get('recordId') or '').strip()
                    claimed_at = Service._timo_reward_epoch_seconds(
                        claim.get('claimed_at') or claim.get('claimed_at_iso')
                        or result.get('checked_at') or result.get('checked_at_iso') or payload_checked_at
                    )
                    claim_cycle_end = Service._timo_reward_task_cycle_end(task)
                    if claim_cycle_end <= 0 and record_id:
                        claim_cycle_end = normalized_record_cycle_ends.get((record_id, target), 0.0)
                    if not cycle_end or target <= 0:
                        continue
                    if claim_cycle_end <= 0 or abs(claim_cycle_end - cycle_end) >= 1.0:
                        continue
                    dedupe_key = f'record:{record_id}' if record_id else f'event:{claimed_at:.3f}:{target}:{reward}:{index}'
                    if dedupe_key in seen[key]:
                        continue
                    seen[key].add(dedupe_key)
                    event = {
                        'claimed_at': claimed_at,
                        'target_diamonds': target,
                        'reward_diamonds': int(reward or 0),
                    }
                    states[key]['claims'].append(event)
        except Exception:
            for state in states.values():
                state['claims'] = []
                state['segmented'] = False
            return states
        for state in states.values():
            state['claims'].sort(key=lambda item: float(item.get('claimed_at') or 0))
        return states

    @staticmethod
    def _timo_reward_status_for_guild(guild_name: str, status: Dict[str, Any]) -> Dict[str, Any]:
        normalized = str(guild_name or '').strip().lower()
        for result in status.get('results') or []:
            if not isinstance(result, dict):
                continue
            names = [
                result.get('account'),
                result.get('guild_name'),
                result.get('guild'),
                result.get('executor_name'),
            ]
            if any(str(name or '').strip().lower() == normalized for name in names):
                return result
        return {}

    @staticmethod
    def _timo_reward_result_names(result: Dict[str, Any]) -> List[str]:
        return [
            str(name or '').strip().lower()
            for name in (
                result.get('account'),
                result.get('guild_name'),
                result.get('guild'),
                result.get('executor_name'),
            )
            if str(name or '').strip()
        ]

    @staticmethod
    def _load_timo_reward_last_task_snapshots() -> Dict[str, Dict[str, Any]]:
        snapshots: Dict[str, Dict[str, Any]] = {}

        def absorb(payload: Any) -> None:
            if not isinstance(payload, dict):
                return
            checked_at_iso = str(payload.get('checked_at_iso') or '').strip()
            for result in payload.get('results') or []:
                if not isinstance(result, dict):
                    continue
                tasks = [task for task in (result.get('last_tasks') or result.get('tasks') or []) if isinstance(task, dict)]
                if not tasks:
                    continue
                tasks_by_key: Dict[str, Dict[str, Any]] = {}
                for index, task in enumerate(tasks):
                    if Service._timo_reward_task_progress(task) > 0 or Service._timo_reward_task_target(task) > 0:
                        tasks_by_key[Service._timo_reward_track_key(task, index)] = task
                if not tasks_by_key:
                    continue
                snapshot = {
                    'checked_at_iso': str(result.get('checked_at_iso') or checked_at_iso).strip(),
                    'tasks_by_key': tasks_by_key,
                }
                for name in Service._timo_reward_result_names(result):
                    snapshots[name] = snapshot

        try:
            history_index = _load_timo_reward_history_index()
            for payload in history_index.get('payloads') or ():
                absorb(payload)
            if TIMO_REWARD_CLAIM_STATE_PATH.exists():
                absorb(json.loads(TIMO_REWARD_CLAIM_STATE_PATH.read_text(encoding='utf-8')))
        except Exception:
            return snapshots
        return snapshots

    def _load_timo_reward_observations(self, guild_name: str) -> Dict[str, Any]:
        normalized_guild = str(guild_name or '').strip().lower()
        observed_targets: Dict[str, set[int]] = {'guild_task': set(), 'quality_host': set()}
        observed_rewards: Dict[str, Dict[int, int]] = {'guild_task': {}, 'quality_host': {}}
        reward_seen_at: Dict[str, Dict[int, str]] = {'guild_task': {}, 'quality_host': {}}
        latest_tasks: Dict[str, Dict[str, Any]] = {}
        latest_meta: Dict[str, Dict[str, str]] = {}
        record_cycle_ends: Dict[Tuple[str, int], float] = {}

        def absorb_task(task: Dict[str, Any], index: int, *, checked_at_iso: str, source: str) -> None:
            key = Service._timo_reward_track_key(task, index)
            if key not in observed_targets:
                return
            target = int(Service._timo_reward_task_target(task))
            progress = Service._timo_reward_task_progress(task)
            reward = Service._timo_reward_task_reward(task)
            checked = str(checked_at_iso or '').strip()
            record_id = str(task.get('record_id') or task.get('recordId') or '').strip()
            exact_cycle_end = Service._timo_reward_task_cycle_end(task)
            if record_id and target > 0 and exact_cycle_end > 0:
                record_cycle_ends[(record_id, target)] = exact_cycle_end
            if target > 0:
                observed_targets[key].add(target)
                previous_checked = reward_seen_at[key].get(target, '')
                if reward and checked >= previous_checked:
                    observed_rewards[key][target] = int(reward)
                    reward_seen_at[key][target] = checked
            current_task = dict(task)
            previous_task = latest_tasks.get(key) if isinstance(latest_tasks.get(key), dict) else {}
            previous_target = int(Service._timo_reward_task_target(previous_task))
            previous_progress = Service._timo_reward_task_progress(previous_task)
            current_cycle_end = Service._timo_reward_task_cycle_end(current_task)
            previous_cycle_end = Service._timo_reward_task_cycle_end(previous_task)
            previous_latest = str((latest_meta.get(key) or {}).get('checked_at_iso') or '')
            current_checked_at = Service._timo_reward_epoch_seconds(checked)
            previous_checked_at = Service._timo_reward_epoch_seconds(previous_latest)
            if previous_task and target == previous_target:
                if (
                    not current_cycle_end
                    and previous_cycle_end >= current_checked_at > 0
                    and progress >= previous_progress
                ):
                    current_task['end_time'] = previous_task.get('end_time') or previous_task.get('endTime')
                elif (
                    current_cycle_end > previous_cycle_end
                    and current_cycle_end >= previous_checked_at > 0
                    and progress <= previous_progress
                ):
                    previous_task['end_time'] = current_task.get('end_time') or current_task.get('endTime')
            if (target > 0 or progress > 0) and checked >= previous_latest:
                latest_tasks[key] = current_task
                latest_meta[key] = {'checked_at_iso': checked, 'source': source}

        def absorb_payload(payload: Any, *, source: str) -> None:
            if not isinstance(payload, dict):
                return
            payload_checked_at = str(payload.get('checked_at_iso') or '').strip()
            for result in payload.get('results') or []:
                if not isinstance(result, dict) or normalized_guild not in Service._timo_reward_result_names(result):
                    continue
                checked_at_iso = str(result.get('checked_at_iso') or payload_checked_at).strip()
                for index, task in enumerate(result.get('last_tasks') or result.get('tasks') or []):
                    if isinstance(task, dict):
                        absorb_task(task, index, checked_at_iso=checked_at_iso, source=source)

        try:
            history_index = _load_timo_reward_history_index()
            for entry in (history_index.get('entries_by_guild') or {}).get(normalized_guild, ()):
                if not isinstance(entry, dict) or not isinstance(entry.get('result'), dict):
                    continue
                absorb_payload(
                    {
                        'checked_at_iso': entry.get('payload_checked_at'),
                        'results': [entry['result']],
                    },
                    source='claim_history_snapshot',
                )
            if TIMO_REWARD_CLAIM_STATE_PATH.exists():
                absorb_payload(json.loads(TIMO_REWARD_CLAIM_STATE_PATH.read_text(encoding='utf-8')), source='claim_status')
        except Exception:
            pass

        db = getattr(self, 'db', None)
        if db is not None and normalized_guild:
            try:
                with db.connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT snapshot_at, task_type, task_name, target_diamonds,
                               progress_diamonds, reward_diamonds, task_status, source_payload
                        FROM timo_external_guild_task_snapshots
                        WHERE LOWER(guild_name) = ?
                          AND task_type IN ('guild_task', 'quality_host')
                        ORDER BY snapshot_at ASC
                        """,
                        (normalized_guild,),
                    ).fetchall()
                for row in rows:
                    item = dict(row)
                    task_type = str(item.get('task_type') or '').strip()
                    try:
                        source_payload = json.loads(str(item.get('source_payload') or '{}'))
                    except json.JSONDecodeError:
                        source_payload = {}
                    if not isinstance(source_payload, dict):
                        source_payload = {}
                    task = {
                        'index': 1 if task_type == 'quality_host' else 0,
                        'main_task': source_payload.get('mainTask') or (10001 if task_type == 'quality_host' else 30),
                        'task_name': str(item.get('task_name') or '').strip(),
                        'task_target': item.get('target_diamonds'),
                        'task_progress': item.get('progress_diamonds'),
                        'task_status': source_payload.get('taskStatus') if 'taskStatus' in source_payload else item.get('task_status'),
                        'reward_diamonds': item.get('reward_diamonds'),
                        'record_id': source_payload.get('recordId') or source_payload.get('record_id'),
                        'end_time': source_payload.get('endTime'),
                    }
                    absorb_task(
                        task,
                        int(task['index']),
                        checked_at_iso=str(item.get('snapshot_at') or ''),
                        source='external_task_snapshot',
                    )
            except Exception:
                pass

        return {
            'targets': {key: sorted(values) for key, values in observed_targets.items()},
            'rewards': observed_rewards,
            'latest_tasks': latest_tasks,
            'latest_meta': latest_meta,
            'record_cycle_ends': record_cycle_ends,
        }

    def _build_timo_reward_track_payload(
        self,
        *,
        key: str,
        title: str,
        subtitle: str,
        tiers_wan: Iterable[int],
        task: Dict[str, Any],
        known_rewards: Dict[int, int],
        checked_at_iso: str,
        claimed_rewards: Optional[Dict[int, int]] = None,
        claimed_targets: Optional[Iterable[int]] = None,
        observed_rewards: Optional[Dict[int, int]] = None,
        catalog_rewards: Optional[Dict[int, int]] = None,
        sync_state: Optional[str] = None,
        prefer_task_target: bool = False,
        progress_offset_diamonds: float = 0.0,
        target_override_diamonds: float = 0.0,
        progress_mode: str = 'cumulative',
        mapping_conflict: bool = False,
    ) -> Dict[str, Any]:
        observed_rewards = observed_rewards or {}
        catalog_rewards = catalog_rewards or {}
        verified_rewards = known_rewards if claimed_rewards is None else claimed_rewards
        verified_targets = {
            int(target)
            for target in (verified_rewards if claimed_targets is None else claimed_targets)
            if int(target or 0) > 0
        }
        raw_progress = self._timo_reward_task_progress(task)
        task_target = self._timo_reward_task_target(task)
        progress_offset = max(0.0, float(progress_offset_diamonds or 0))
        progress = progress_offset + raw_progress
        tiers = [
            {
                'wan': int(wan),
                'diamonds': int(wan) * 10000,
                'reward_diamonds': verified_rewards.get(int(wan) * 10000) or known_rewards.get(int(wan) * 10000) or observed_rewards.get(int(wan) * 10000) or catalog_rewards.get(int(wan) * 10000),
                'reward_claim_verified': int(wan) * 10000 in verified_targets,
                'reward_source': 'claim_receipt' if verified_rewards.get(int(wan) * 10000) or known_rewards.get(int(wan) * 10000) else ('timo_task_snapshot' if observed_rewards.get(int(wan) * 10000) else ('configured_reward_catalog' if catalog_rewards.get(int(wan) * 10000) else 'unknown')),
            }
            for wan in tiers_wan
        ]
        target_override = max(0.0, float(target_override_diamonds or 0))
        target = target_override or (task_target if prefer_task_target and task_target > 0 else next((float(t['diamonds']) for t in tiers if float(t['diamonds']) > progress), 0.0))
        if target <= 0:
            target = task_target or float(tiers[-1]['diamonds'] if tiers else 0)
        snapshot_reward = self._timo_reward_task_reward(task)
        snapshot_target = target if target_override > 0 else task_target
        if snapshot_reward and snapshot_target > 0:
            for tier in tiers:
                if int(tier['diamonds']) == int(snapshot_target) and tier.get('reward_diamonds') is None:
                    tier['reward_diamonds'] = snapshot_reward
                    tier['reward_source'] = 'current_timo_task'
        current_tier = next((tier for tier in tiers if int(tier['diamonds']) == int(target)), None)
        current_reward = current_tier.get('reward_diamonds') if current_tier else None
        task_status = int(self._timo_reward_number(task.get('task_status')))
        for tier in tiers:
            tier_diamonds = int(tier['diamonds'])
            if tier.get('reward_claim_verified'):
                tier['reward_state'] = 'claimed'
            elif target == tier_diamonds and task_status:
                tier['reward_state'] = 'claimable'
            elif tier_diamonds <= progress:
                tier['reward_state'] = 'progressed'
            elif target == tier_diamonds:
                tier['reward_state'] = 'current'
            else:
                tier['reward_state'] = 'upcoming'
        percent = min(100.0, max(0.0, progress / target * 100.0)) if target > 0 else 0.0
        return {
            'key': key,
            'title': title,
            'subtitle': subtitle,
            'unit': '万钻石',
            'tiers': tiers,
            'progress_diamonds': progress,
            'target_diamonds': target,
            'raw_progress_diamonds': raw_progress,
            'raw_target_diamonds': task_target,
            'progress_offset_diamonds': progress_offset,
            'progress_mode': str(progress_mode or 'cumulative'),
            'mapping_conflict': bool(mapping_conflict),
            'percent_to_target': round(percent, 2),
            'process': str(task.get('process') or '').strip(),
            'task_status': task.get('task_status'),
            'reward_diamonds': current_reward,
            'checked_at_iso': checked_at_iso,
            'sync_state': sync_state or ('ok' if task else 'missing'),
        }

    def _build_timo_reward_tracks(self, guild_name: str, *, country: str = '') -> List[Dict[str, Any]]:
        status = self._load_timo_reward_claim_status()
        history_rewards = self._load_timo_reward_claim_history_rewards(guild_name)
        observations = self._load_timo_reward_observations(guild_name)
        tasks_by_key = observations.get('latest_tasks') if isinstance(observations.get('latest_tasks'), dict) else {}
        latest_meta = observations.get('latest_meta') if isinstance(observations.get('latest_meta'), dict) else {}
        observed_rewards = observations.get('rewards') if isinstance(observations.get('rewards'), dict) else {}
        record_cycle_ends = observations.get('record_cycle_ends') if isinstance(observations.get('record_cycle_ends'), dict) else {}
        guild_status = self._timo_reward_status_for_guild(guild_name, status)
        checked_at_iso = str(status.get('checked_at_iso') or guild_status.get('checked_at_iso') or '').strip()
        normalized_country = str(country or '').strip().lower()
        use_mexico_catalog = normalized_country in {'mexico', 'méxico'}
        use_brazil_catalog = normalized_country in {'brazil', 'brasil'}
        use_indonesia_catalog = normalized_country == 'indonesia'
        prefer_task_target = use_mexico_catalog or use_brazil_catalog or use_indonesia_catalog
        cycle_end_by_key = {
            key: self._timo_reward_task_cycle_end(task)
            for key, task in tasks_by_key.items()
            if isinstance(task, dict)
            and self._timo_reward_task_cycle_end(task) > 0
        }
        current_claim_states = (
            self._load_timo_reward_current_cycle_claims(guild_name, cycle_end_by_key, record_cycle_ends)
            if cycle_end_by_key
            else {}
        )
        mexico_catalog_rewards = {
            tier_wan * 10000: int(round(reward_wan * 10000))
            for tier_wan, reward_wan in TIMO_MEXICO_GUILD_TASK_REWARD_WAN.items()
            if tier_wan > 0
        }
        brazil_catalog_rewards = {
            tier_wan * 10000: int(round(reward_wan * 10000))
            for tier_wan, reward_wan in TIMO_BRAZIL_GUILD_TASK_REWARD_WAN.items()
            if tier_wan > 0
        }
        indonesia_catalog_rewards = {
            tier_wan * 10000: reward_wan * 10000
            for tier_wan, reward_wan in TIMO_INDONESIA_GUILD_TASK_REWARD_WAN.items()
            if tier_wan > 0
        }

        def track_tiers_wan(key: str, fallback_tiers: Iterable[int]) -> Tuple[int, ...]:
            if use_mexico_catalog and key == 'guild_task':
                return tuple(tier_wan for tier_wan in TIMO_MEXICO_GUILD_TASK_REWARD_WAN if tier_wan > 0)
            if use_brazil_catalog and key == 'guild_task':
                return tuple(tier_wan for tier_wan in TIMO_BRAZIL_GUILD_TASK_REWARD_WAN if tier_wan > 0)
            if use_indonesia_catalog and key == 'guild_task':
                return tuple(tier_wan for tier_wan in TIMO_INDONESIA_GUILD_TASK_REWARD_WAN if tier_wan > 0)
            return tuple(int(value) for value in fallback_tiers)

        def task_payload_args(key: str) -> Dict[str, Any]:
            current_task = tasks_by_key.get(key) if isinstance(tasks_by_key.get(key), dict) else None
            meta = latest_meta.get(key) if isinstance(latest_meta.get(key), dict) else {}
            if current_task:
                source = str(meta.get('source') or '')
                return {
                    'task': current_task,
                    'checked_at_iso': str(meta.get('checked_at_iso') or checked_at_iso),
                    'sync_state': 'stale' if source == 'claim_history_snapshot' else 'ok',
                }
            return {'task': {}, 'checked_at_iso': checked_at_iso, 'sync_state': 'missing'}

        def claim_projection_args(key: str) -> Dict[str, Any]:
            state = current_claim_states.get(key) if isinstance(current_claim_states.get(key), dict) else {}
            claims = [item for item in (state.get('claims') or []) if isinstance(item, dict)]
            claimed_rewards: Dict[int, int] = {}
            tier_targets = {
                int(tier_wan) * 10000
                for tier_wan in track_tiers_wan(
                    key,
                    TIMO_GUILD_TASK_TIER_WAN if key == 'guild_task' else TIMO_QUALITY_HOST_TASK_TIER_WAN,
                )
            }
            claimed_targets: set[int] = set()
            for claim in claims:
                target = int(claim.get('target_diamonds') or 0)
                catalog_reward = (
                    mexico_catalog_rewards.get(target)
                    if use_mexico_catalog and key == 'guild_task'
                    else brazil_catalog_rewards.get(target)
                    if use_brazil_catalog and key == 'guild_task'
                    else indonesia_catalog_rewards.get(target)
                    if use_indonesia_catalog and key == 'guild_task'
                    else 0
                )
                reward = int(catalog_reward or claim.get('reward_diamonds') or 0)
                if target in tier_targets:
                    claimed_targets.add(target)
                if target in tier_targets and reward > 0:
                    claimed_rewards[target] = reward
            if key == 'guild_task' and claimed_targets:
                claimed_through = max(claimed_targets)
                claimed_targets.update(target for target in tier_targets if target <= claimed_through)
            return {
                'claimed_rewards': claimed_rewards,
                'claimed_targets': claimed_targets,
            }

        guild_history_rewards = history_rewards.get('guild_task') or {}
        if use_mexico_catalog:
            guild_known_rewards = {}
            guild_observed_rewards = {}
            guild_catalog_rewards = mexico_catalog_rewards
        elif use_brazil_catalog:
            guild_known_rewards = {
                target: brazil_catalog_rewards.get(target, reward)
                for target, reward in guild_history_rewards.items()
            }
            guild_observed_rewards: Dict[int, int] = {}
            guild_catalog_rewards = brazil_catalog_rewards
        elif use_indonesia_catalog:
            guild_known_rewards = {}
            guild_observed_rewards = {}
            guild_catalog_rewards = indonesia_catalog_rewards
        else:
            guild_known_rewards = guild_history_rewards
            guild_observed_rewards = observed_rewards.get('guild_task') or {}
            guild_catalog_rewards = {}

        guild_tiers_wan = track_tiers_wan('guild_task', TIMO_GUILD_TASK_TIER_WAN)
        tracks = [
            self._build_timo_reward_track_payload(
                key='guild_task',
                title='公会任务',
                subtitle='按公会收益进度解锁多档钻石奖励',
                tiers_wan=guild_tiers_wan,
                **task_payload_args('guild_task'),
                **claim_projection_args('guild_task'),
                known_rewards=guild_known_rewards,
                observed_rewards=guild_observed_rewards,
                catalog_rewards=guild_catalog_rewards,
                prefer_task_target=prefer_task_target,
            )
        ]
        if not use_brazil_catalog and 'quality_host' in tasks_by_key:
            quality_tiers_wan = TIMO_QUALITY_HOST_TASK_TIER_WAN
            tracks.append(
                self._build_timo_reward_track_payload(
                    key='quality_host',
                    title='公会优质主播达标收益佣金',
                    subtitle='单档 400 万钻石，达成后自动领取',
                    tiers_wan=quality_tiers_wan,
                    **task_payload_args('quality_host'),
                    **claim_projection_args('quality_host'),
                    known_rewards=history_rewards.get('quality_host') or {},
                    observed_rewards=observed_rewards.get('quality_host') or {},
                    prefer_task_target=prefer_task_target,
                )
            )
        return tracks

    def _empty_timo_auth_station_status(self, guild_name: str) -> Dict[str, Any]:
        from app.timo_guild_identity import timo_guild_display_name

        return {
            'guild_name': str(guild_name or '').strip(),
            'guild_display_name': timo_guild_display_name(guild_name),
            'status': 'not_bound',
            'running': False,
            'label': '自动取码未绑定',
            'station_id': '',
            'device_serial': '',
            'last_heartbeat_at': '',
            'heartbeat_age_seconds': None,
            'device_status': '',
            'adb_status': '',
            'app_status': '',
            'page_status': '',
            'relay_version': '',
            'last_error_code': '',
            'last_error_message': '',
            'device_ready': False,
            'device_ready_label': '设备未就绪',
            'device_ready_reasons': ['未绑定设备'],
            'transport_ready': False,
            'observation_ready': False,
            'observation_ready_label': '观察未就绪',
            'observation_ready_reasons': ['未绑定设备'],
            'otp_ready': False,
            'otp_ready_label': '取码未就绪',
            'otp_ready_reasons': ['未绑定设备'],
            'blocked_reason': 'device_binding_missing',
        }

    def _timo_auth_station_status_by_guild(self) -> Dict[str, Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        try:
            with self.db.connect() as conn:
                rows = [dict(row) for row in conn.execute(
                    """
                    SELECT
                        b.guild_name,
                        b.station_id,
                        b.device_serial,
                        b.account_fingerprint,
                        b.updated_at AS binding_updated_at,
                        COALESCE(h.status, s.status) AS station_status,
                        COALESCE(h.last_heartbeat_at, s.last_heartbeat_at) AS last_heartbeat_at,
                        COALESCE(h.device_status, s.device_status) AS device_status,
                        COALESCE(h.adb_status, s.adb_status) AS adb_status,
                        COALESCE(h.app_status, s.app_status) AS app_status,
                        COALESCE(h.page_status, s.page_status) AS page_status,
                        COALESCE(h.relay_version, s.relay_version) AS relay_version,
                        COALESCE(h.last_error_code, s.last_error_code) AS last_error_code,
                        COALESCE(h.last_error_message, s.last_error_message) AS last_error_message,
                        h.screen_unlocked,
                        h.timo_app_installed,
                        h.notification_permission_enabled,
                        h.notification_listener_enabled,
                        h.accessibility_enabled,
                        h.network_connected,
                        h.official_assistant_page_ready,
                        h.last_successful_ui_dump_at,
                        h.device_health,
                        h.locator_profile_status,
                        h.ui_probe_status,
                        h.last_dump_error,
                        h.last_dump_error_at,
                        h.observation_ready,
                        h.observation_ready_at
                    FROM timo_auth_station_device_bindings b
                    LEFT JOIN timo_auth_station_device_heartbeats h
                      ON h.station_id = b.station_id AND h.device_id = b.device_serial
                    LEFT JOIN timo_auth_stations s ON s.station_id = b.station_id
                    WHERE b.status = 'active'
                    ORDER BY b.updated_at DESC
                    """
                ).fetchall()]
        except sqlite3.Error:
            return {}
        statuses: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            guild_name = str(row.get('guild_name') or '').strip()
            guild_key = guild_name.lower()
            if not guild_key or guild_key in statuses:
                continue
            from app.timo_guild_identity import timo_guild_display_name
            row['guild_display_name'] = timo_guild_display_name(guild_name)
            heartbeat_at = str(row.get('last_heartbeat_at') or '').strip()
            heartbeat_age_seconds: Optional[int] = None
            if heartbeat_at:
                try:
                    heartbeat_age_seconds = max(0, int((now - parse_iso_datetime(heartbeat_at)).total_seconds()))
                except Exception:
                    heartbeat_age_seconds = None
            station_status = str(row.get('station_status') or '').strip().lower()
            heartbeat_fresh = heartbeat_age_seconds is not None and heartbeat_age_seconds <= 120
            if heartbeat_fresh and station_status == 'online':
                status_code = 'running'
                label = '自动取码运行中'
                running = True
            elif heartbeat_at:
                status_code = 'stale'
                label = '自动取码失效'
                running = False
            else:
                status_code = 'not_running'
                label = '自动取码未运行'
                running = False
            readiness = _station_device_readiness({
                'status': station_status,
                'last_heartbeat_at': heartbeat_at,
                'adb_status': row.get('adb_status'),
                'app_status': row.get('app_status'),
                'screen_unlocked': row.get('screen_unlocked'),
                'timo_app_installed': row.get('timo_app_installed'),
                'notification_permission_enabled': row.get('notification_permission_enabled'),
                'notification_listener_enabled': row.get('notification_listener_enabled'),
                'accessibility_enabled': row.get('accessibility_enabled'),
                'network_connected': row.get('network_connected'),
                'official_assistant_page_ready': row.get('official_assistant_page_ready'),
                'last_successful_ui_dump_at': row.get('last_successful_ui_dump_at'),
                'device_health': row.get('device_health'),
                'locator_profile_status': row.get('locator_profile_status'),
                'ui_probe_status': row.get('ui_probe_status'),
                'last_dump_error': row.get('last_dump_error'),
                'last_dump_error_at': row.get('last_dump_error_at'),
            }, now=now)
            statuses[guild_key] = {
                'guild_name': guild_name,
                'guild_display_name': row['guild_display_name'],
                'status': status_code,
                'running': running,
                'label': label,
                'station_id': str(row.get('station_id') or '').strip(),
                'device_serial': str(row.get('device_serial') or '').strip(),
                'last_heartbeat_at': heartbeat_at,
                'heartbeat_age_seconds': heartbeat_age_seconds,
                'device_status': str(row.get('device_status') or '').strip(),
                'adb_status': str(row.get('adb_status') or '').strip(),
                'app_status': str(row.get('app_status') or '').strip(),
                'page_status': str(row.get('page_status') or '').strip(),
                'relay_version': str(row.get('relay_version') or '').strip(),
                'last_error_code': str(row.get('last_error_code') or '').strip(),
                'last_error_message': str(row.get('last_error_message') or '').strip(),
                **readiness,
            }
        return statuses

    def _normalize_timo_executor_keepalive_entry(self, item: Dict[str, Any]) -> Dict[str, Any]:
        entry = dict(item or {})
        checked_at_ts: Optional[int] = None
        raw_checked = entry.get('checked_at')
        if isinstance(raw_checked, (int, float)):
            checked_at_ts = int(raw_checked)
        else:
            raw_iso = str(entry.get('checked_at_iso') or raw_checked or '').strip()
            if raw_iso:
                try:
                    checked_at_ts = int(parse_iso_datetime(raw_iso).timestamp())
                except Exception:
                    checked_at_ts = None
        stale_after_seconds = _coerce_positive_int(entry.get('stale_after_seconds'), TIMO_EXECUTOR_KEEPALIVE_STALE_UNKNOWN_SECONDS)
        is_stale = checked_at_ts is None or (time.time() - checked_at_ts) > stale_after_seconds
        error_category = str(entry.get('error_category') or '').strip().lower()
        capability = str(entry.get('capability') or '').strip().lower()
        ok = bool(entry.get('ok'))
        if is_stale:
            live_status = 'unknown'
        elif ok or capability == 'timo_ticket_active':
            live_status = 'active'
        elif error_category in {'auth_invalid', 'target_not_visible', 'not_configured'} or capability in {'timo_ticket_invalid', 'timo_target_not_visible'}:
            live_status = 'inactive'
        else:
            live_status = 'unknown'
        reason_map = {
            'auth_invalid': 'Timo Ticket 已失效，请更新',
            'target_not_visible': 'Timo Ticket 无法访问目标公会',
            'not_configured': '未配置 Timo Ticket',
            'transient_timeout': 'Timo 探活超时，待校验',
            'transient_network': 'Timo 网络波动，待校验',
            'http_error': 'Timo 校验异常，待校验',
            'timo_api_rejected': 'Timo 返回拒绝，待处理',
            'unknown': 'Timo Ticket 待校验',
        }
        normalized_reason = 'Timo Ticket 待校验' if is_stale else (reason_map.get(error_category) or ('' if ok else str(entry.get('error') or 'Timo Ticket 待校验')))
        entry['checked_at'] = checked_at_ts
        entry['checked_at_iso'] = str(entry.get('checked_at_iso') or (datetime.fromtimestamp(checked_at_ts, tz=timezone.utc).isoformat().replace('+00:00', 'Z') if checked_at_ts else ''))
        entry['stale_after_seconds'] = stale_after_seconds
        entry['is_stale'] = is_stale
        entry['live_status'] = live_status
        entry['normalized_reason'] = normalized_reason
        entry['probe_endpoint'] = str(entry.get('probe_endpoint') or '')
        entry['error_category'] = error_category
        entry['capability'] = capability
        return entry

    def _timo_executor_keepalive_status_by_guild(self) -> Dict[str, Dict[str, Any]]:
        try:
            if not TIMO_EXECUTOR_KEEPALIVE_STATUS_PATH.exists():
                return {}
            payload = json.loads(TIMO_EXECUTOR_KEEPALIVE_STATUS_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
        rows = payload.get('results') if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized = self._normalize_timo_executor_keepalive_entry(item)
            guild_name = str(normalized.get('guild_name') or normalized.get('account') or '').strip()
            if guild_name:
                result[guild_name.lower()] = normalized
        return result

    def list_timo_guild_executors(
        self,
        *,
        user: Optional[Dict[str, Any]] = None,
        include_reward_tracks: bool = True,
    ) -> Dict[str, Any]:
        from app.timo_guild_identity import timo_guild_display_name

        rows = self.list_guild_executors(app_name='timo').get('rows', [])
        auth_station_by_guild = self._timo_auth_station_status_by_guild()
        ticket_status_by_guild = self._timo_executor_keepalive_status_by_guild()
        visible_rows: List[Dict[str, Any]] = []
        for row in rows:
            guild_name = str(row.get('guild_name') or '').strip()
            if not guild_name or not self._ops_intake_user_can_access_guild(user, guild_name):
                continue
            public = dict(row)
            public['guild_display_name'] = timo_guild_display_name(
                guild_name,
                guild_id=public.get('cms_guild_id'),
                guild_sid=public.get('cms_guild_sid'),
            )
            guild_contract = timo_guild_contract_fields(
                guild_name,
                guild_id=public.get('cms_guild_id'),
                guild_sid=public.get('cms_guild_sid'),
            )
            public['guild_id'] = guild_contract.get('guild_id', str(public.get('cms_guild_id') or ''))
            public['guild_sid'] = guild_contract.get('guild_sid', str(public.get('cms_guild_sid') or ''))
            public['platform_backend_url'] = str(public.get('platform_backend_url') or TIMO_DEFAULT_API_BASE_URL).strip() or TIMO_DEFAULT_API_BASE_URL
            public['cms_guild_sid'] = str(public.get('cms_guild_sid') or public.get('cms_guild_id') or '').strip()
            public['bind_concurrency'] = max(1, int(public.get('bind_concurrency') or 3))
            public['request_timeout_seconds'] = max(3, int(public.get('request_timeout_seconds') or 15))
            public['cms_token_configured'] = bool(public.get('platform_authorization_configured'))
            public['cms_refresh_token_configured'] = False
            public['oauth_configured'] = False
            public['assignees'] = self._ops_intake_assignees_for_guild(guild_name)
            if include_reward_tracks:
                public['reward_tracks'] = self._build_timo_reward_tracks(guild_name, country=str(public.get('guild_country') or public.get('country') or ''))
            public['auth_station_status'] = auth_station_by_guild.get(guild_name.lower()) or self._empty_timo_auth_station_status(guild_name)
            ticket_status = ticket_status_by_guild.get(guild_name.lower()) or {}
            timo_live_status = str(ticket_status.get('live_status') or ('not_configured' if not public['cms_token_configured'] else 'unknown')).strip() or 'unknown'
            public['timo_live_status'] = timo_live_status
            public['timo_live_checked_at'] = ticket_status.get('checked_at_iso') or ticket_status.get('checked_at')
            public['timo_live_error'] = ticket_status.get('error')
            public['timo_live_error_category'] = ticket_status.get('error_category')
            public['timo_live_reason'] = ticket_status.get('normalized_reason') or ''
            public['timo_live_capability'] = ticket_status.get('capability') or ''
            public['timo_live_is_stale'] = bool(ticket_status.get('is_stale'))
            public['timo_account_diamond_balance'] = ticket_status.get('account_diamond_balance')
            public['timo_account_balance_checked_at'] = ticket_status.get('checked_at_iso') or ticket_status.get('checked_at')
            public['cms_channel_status'] = 'valid' if timo_live_status == 'active' else ('invalid' if timo_live_status == 'inactive' else ('not_configured' if not public['cms_token_configured'] else 'unknown'))
            visible_rows.append(public)
        return {'rows': visible_rows}

    def trigger_timo_guild_executor_health_refresh(self, *, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Start one non-blocking keepalive run when a Timo status is unknown/stale.

        The runner owns the cross-process flock and the SQLite job lock. The API
        only starts it and returns immediately, so loading the operator page is
        never held up by Timo, Chrome, or OTP recovery.
        """
        rows = self.list_timo_guild_executors(user=user, include_reward_tracks=False).get('rows', [])
        eligible = [
            str(row.get('guild_name') or '').strip()
            for row in rows
            if bool(row.get('platform_authorization_configured'))
            and str(row.get('timo_live_status') or '').strip() in {'unknown', ''}
        ]
        eligible = [name for name in eligible if name]
        if not eligible:
            return {'ok': True, 'queued': False, 'reason': 'timo_ticket_status_already_known', 'guild_names': []}
        runner = TIMO_KEEPALIVE_RUNNER_PATH
        if not runner.exists():
            return {'ok': False, 'queued': False, 'reason': 'timo_keepalive_runner_missing', 'guild_names': eligible}
        try:
            process = subprocess.Popen(
                ['/bin/bash', str(runner)],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                env=os.environ.copy(),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                'ok': False,
                'queued': False,
                'reason': 'timo_keepalive_start_failed',
                'error': str(exc)[:200],
                'guild_names': eligible,
            }
        return {
            'ok': True,
            'queued': True,
            'pid': int(process.pid),
            'reason': 'timo_keepalive_started',
            'guild_names': eligible,
        }

    def list_sogo_guild_executors(self, *, user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rows_by_guild: Dict[str, Dict[str, Any]] = {}
        for app_name in SUGO_APP_NAMES:
            for row in self.list_guild_executors(app_name=app_name).get('rows', []):
                rows_by_guild.setdefault(str(row.get('guild_name') or ''), row)
        rows = list(rows_by_guild.values())
        visible_rows: List[Dict[str, Any]] = []
        for row in rows:
            guild_name = str(row.get('guild_name') or '').strip()
            if not guild_name or not self._ops_intake_user_can_access_guild(user, guild_name):
                continue
            public = dict(row)
            public['app_name'] = SUGO_APP_NAME
            public['platform_backend_url'] = str(public.get('platform_backend_url') or SUGO_DEFAULT_API_BASE_URL).strip() or SUGO_DEFAULT_API_BASE_URL
            public['enabled'] = bool(public.get('enabled'))
            public['bind_concurrency'] = max(1, int(public.get('bind_concurrency') or 1))
            public['request_timeout_seconds'] = max(5, int(public.get('request_timeout_seconds') or 30))
            public['cms_token_configured'] = bool(public.get('platform_authorization_configured'))
            public['oauth_configured'] = False
            public['assignees'] = self._ops_intake_assignees_for_guild(guild_name)
            visible_rows.append(public)
        return {'rows': visible_rows}

    def _official_group_bridge_console_base_url(self) -> Optional[str]:
        webhook_url = str(self.official_group_approval_webhook_url or '').strip()
        if not webhook_url:
            return None
        if webhook_url.endswith('/official-group/approve'):
            return webhook_url[:-len('/official-group/approve')]
        return webhook_url.rstrip('/')

    @staticmethod
    def _official_group_bridge_candidate_base_urls(base_url: Optional[str]) -> list[str]:
        ordered: list[str] = []
        for candidate in [str(base_url or '').strip().rstrip('/'), OFFICIAL_GROUP_BRIDGE_DEFAULT_BASE_URL]:
            if candidate and candidate not in ordered:
                ordered.append(candidate)
        return ordered

    def _request_official_group_bridge_json(self, url: str) -> Dict[str, Any]:
        token = str(self.official_group_bridge_token or os.getenv('OFFICIAL_GROUP_BRIDGE_TOKEN') or '').strip()
        headers = {'Authorization': f'Bearer {token}'} if token else None
        response = requests.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        content_type = str(response.headers.get('content-type') or '').lower()
        body_text = response.text
        if 'html' in content_type or body_text.lstrip().startswith('<!doctype html') or body_text.lstrip().startswith('<html'):
            raise ValueError('official group bridge returned html instead of json')
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('official group bridge must return a JSON object')
        return payload

    def _probe_official_group_bridge_base_url(self, base_url: str) -> Dict[str, Any]:
        normalized = str(base_url or '').strip().rstrip('/')
        if not normalized:
            return {
                'base_url': '',
                'health': {'status': 'unreachable', 'error': 'bridge base_url is not configured'},
                'summary': {'status': 'unreachable', 'error': 'bridge base_url is not configured'},
                'ready': False,
            }
        health_url = f'{normalized}/ops/official-group-bridge/health'
        summary_url = f'{normalized}/ops/official-group-bridge/summary'
        try:
            health = self._request_official_group_bridge_json(health_url)
        except Exception as exc:
            health = {'status': 'unreachable', 'error': str(exc)}
        try:
            summary = self._request_official_group_bridge_json(summary_url)
        except Exception as exc:
            summary = {'status': 'unreachable', 'error': str(exc)}
        ready = health.get('status') == 'healthy' and summary.get('status') != 'unreachable'
        return {
            'base_url': normalized,
            'health': health,
            'summary': summary,
            'ready': bool(ready),
        }

    def _start_official_group_bridge_service(self, *, timeout_seconds: float = 30.0) -> Dict[str, Any]:
        script_path = OFFICIAL_GROUP_BRIDGE_START_SCRIPT
        if not script_path.exists():
            raise RuntimeError(f'official group bridge start script not found: {script_path}')
        completed = subprocess.run(
            [str(script_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
        stdout = str(completed.stdout or '').strip()
        stderr = str(completed.stderr or '').strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f'exit code {completed.returncode}'
            raise RuntimeError(f'official group bridge start failed: {detail}')
        return {
            'started': True,
            'stdout': stdout,
            'stderr': stderr,
            'timeout_seconds': float(timeout_seconds),
        }

    def _restart_shared_whatsapp_approval_worker_service(self, *, timeout_seconds: float = 30.0) -> Dict[str, Any]:
        allow_legacy_restart = str(os.getenv('ALLOW_LEGACY_WEBJS_8787') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
        if not allow_legacy_restart:
            raise RuntimeError('legacy WebJS 8787 shared worker restart is disabled; set ALLOW_LEGACY_WEBJS_8787=1 for emergency legacy use')
        script_path = WHATSAPP_APPROVAL_WORKER_RESTART_SCRIPT
        if not script_path.exists():
            raise RuntimeError(f'shared worker restart script not found: {script_path}')
        completed = subprocess.run(
            [str(script_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
        stdout = str(completed.stdout or '').strip()
        stderr = str(completed.stderr or '').strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f'exit code {completed.returncode}'
            raise RuntimeError(f'shared worker restart failed: {detail}')
        return {
            'started': True,
            'stdout': stdout,
            'stderr': stderr,
            'timeout_seconds': float(timeout_seconds),
        }

    def _whatsapp_approval_auto_recover_allowed(self, recover_key: str, *, cooldown_seconds: float = 30.0) -> bool:
        now_ts = time.time()
        with self._whatsapp_approval_auto_recover_lock:
            current = dict(self._whatsapp_approval_auto_recover_state.get(recover_key) or {})
            try:
                last_attempt_ts = float(current.get('last_attempt_ts') or 0.0)
            except (TypeError, ValueError):
                last_attempt_ts = 0.0
            if last_attempt_ts and (now_ts - last_attempt_ts) < max(float(cooldown_seconds), 1.0):
                return False
            current['last_attempt_ts'] = now_ts
            current['last_attempt_at'] = utc_now()
            self._whatsapp_approval_auto_recover_state[recover_key] = current
        return True

    def _whatsapp_approval_auto_recover_record(self, recover_key: str, *, ok: bool, detail: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> None:
        with self._whatsapp_approval_auto_recover_lock:
            current = dict(self._whatsapp_approval_auto_recover_state.get(recover_key) or {})
            current['last_result_ok'] = bool(ok)
            current['last_result_at'] = utc_now()
            current['last_error'] = None if ok else str(detail or '')
            if payload is not None:
                current['last_payload'] = dict(payload or {})
            self._whatsapp_approval_auto_recover_state[recover_key] = current

    def _maybe_auto_recover_whatsapp_approval_account_runtime(
        self,
        row: Dict[str, Any],
        *,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        worker_health: Optional[Dict[str, Any]] = None,
        cooldown_seconds: float = 30.0,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], bool]:
        runtime_state = dict(runtime_state or {})
        session_state = dict(session_state or {})
        worker_health = dict(worker_health or {})
        normalized_key = str((row or {}).get('account_key') or '').strip()
        if not normalized_key or not bool((row or {}).get('enabled')) or not bool((row or {}).get('auto_recover_worker')):
            return runtime_state, session_state, worker_health, False

        login_check_status = str(session_state.get('login_check_status') or '').strip()
        if login_check_status in {'account_restricted', 'auth_failed', 'waiting_for_scan'}:
            return runtime_state, session_state, worker_health, False

        normalized_type = str((row or {}).get('responsible_type') or '').strip()
        has_dedicated_meta = bool(self._read_whatsapp_approval_runtime_meta(normalized_key))

        recover_key = ''
        recovery_mode = ''
        if normalized_type == 'official_group' and (
            str(runtime_state.get('source') or '').strip() != 'dedicated'
            or not bool(runtime_state.get('active'))
            or not bool(session_state.get('login_verified'))
        ):
            recover_key = f'wa-dedicated:{normalized_key}'
            recovery_mode = 'dedicated_runtime_bootstrap'
        elif has_dedicated_meta and not bool(runtime_state.get('active')):
            recover_key = f'wa-dedicated:{normalized_key}'
            recovery_mode = 'dedicated_runtime_rebuild'
        elif str(runtime_state.get('source') or '').strip() == 'unavailable':
            recover_key = 'wa-shared-worker'
            recovery_mode = 'shared_worker_restart'
        else:
            return runtime_state, session_state, worker_health, False

        if not self._whatsapp_approval_auto_recover_allowed(recover_key, cooldown_seconds=cooldown_seconds):
            return runtime_state, session_state, worker_health, False

        try:
            if recovery_mode == 'shared_worker_restart':
                self._restart_shared_whatsapp_approval_worker_service(timeout_seconds=max(float(cooldown_seconds), 30.0))
                recovered_worker_health = self._current_whatsapp_approval_worker_health()
                recovered_runtime = self._build_whatsapp_approval_runtime_state(normalized_key, worker_health=recovered_worker_health)
                recovered_session = self._build_whatsapp_approval_session_state(normalized_key, worker_health=recovered_worker_health if recovered_runtime.get('source') == 'shared' else {}, include_qr_ascii=False)
            else:
                recovered = self.start_whatsapp_approval_account_session(normalized_key)
                recovered_runtime = dict(recovered.get('runtime') or {})
                recovered_session = dict(recovered.get('session') or {})
                recovered_worker_health = dict(worker_health or {})
                recovered_base_url = str(recovered_runtime.get('base_url') or '').strip()
                if recovered_runtime.get('active') and recovered_base_url and str(recovered_runtime.get('source') or '').strip() == 'dedicated':
                    try:
                        recovered_worker_health = self._request_whatsapp_approval_worker_health(recovered_base_url)
                    except Exception:
                        recovered_worker_health = dict(worker_health or {})
            self._whatsapp_approval_auto_recover_record(recover_key, ok=True, payload={'mode': recovery_mode, 'account_key': normalized_key})
            return recovered_runtime, recovered_session, recovered_worker_health, True
        except Exception as exc:
            self._whatsapp_approval_auto_recover_record(recover_key, ok=False, detail=str(exc), payload={'mode': recovery_mode, 'account_key': normalized_key})
            return runtime_state, session_state, worker_health, False

    def _build_whatsapp_approval_account_runtime_with_auto_recover(
        self,
        row: Dict[str, Any],
        *,
        production_ops: Optional[Dict[str, Any]] = None,
        official_bridge: Optional[Dict[str, Any]] = None,
        shared_worker_health: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        production_ops = production_ops or self._production_ops_daemon_snapshot()
        official_bridge = official_bridge or self._official_group_bridge_summary_payload()
        account_key = str((row or {}).get('account_key') or '').strip()
        baileys_runtime_state, baileys_session_state, baileys_used = self._build_baileys_whatsapp_approval_runtime_and_session(
            row,
            include_qr_ascii=False,
        )
        if baileys_used:
            built = self._build_whatsapp_approval_account_runtime(
                row,
                production_ops=production_ops,
                official_bridge=official_bridge,
                worker_health={},
                runtime_state=baileys_runtime_state,
                session_state=baileys_session_state,
            )
            return built, dict(shared_worker_health or {})
        has_dedicated_meta = bool(self._read_whatsapp_approval_runtime_meta(account_key))
        account_worker_health: Dict[str, Any] = {}
        runtime_state = self._build_whatsapp_approval_runtime_state(account_key, worker_health=None if has_dedicated_meta else shared_worker_health)
        if runtime_state.get('source') == 'shared':
            account_worker_health = dict(shared_worker_health or {})
        elif runtime_state.get('active') and runtime_state.get('base_url'):
            try:
                account_worker_health = self._request_whatsapp_approval_worker_health(str(runtime_state.get('base_url') or ''))
                runtime_state = self._build_whatsapp_approval_runtime_state(account_key, worker_health=account_worker_health, allow_shared_fallback=False)
            except Exception:
                account_worker_health = {}
        session_state = self._build_whatsapp_approval_session_state(account_key, worker_health=account_worker_health, include_qr_ascii=False)
        runtime_state, session_state, account_worker_health, recovered_runtime = self._maybe_auto_recover_whatsapp_approval_account_runtime(
            row,
            runtime_state=runtime_state,
            session_state=session_state,
            worker_health=account_worker_health,
        )
        if recovered_runtime and runtime_state.get('source') == 'shared':
            shared_worker_health = dict(account_worker_health or {})
        runtime_state, session_state, account_worker_health, _ = self._maybe_auto_recover_whatsapp_approval_account_session(
            account_key,
            runtime_state=runtime_state,
            session_state=session_state,
            worker_health=account_worker_health,
        )
        built = self._build_whatsapp_approval_account_runtime(
            row,
            production_ops=production_ops,
            official_bridge=official_bridge,
            worker_health=account_worker_health,
            runtime_state=runtime_state,
            session_state=session_state,
        )
        return built, dict(shared_worker_health or {})

    def _maybe_auto_recover_official_group_bridge(self, *, cooldown_seconds: float = 30.0) -> bool:
        now_ts = time.time()
        with self._official_group_bridge_recover_lock:
            last_attempt_ts = 0.0
            try:
                last_attempt_ts = float(self._official_group_bridge_recover_state.get('last_attempt_ts') or 0.0)
            except (TypeError, ValueError):
                last_attempt_ts = 0.0
            if last_attempt_ts and (now_ts - last_attempt_ts) < max(float(cooldown_seconds), 1.0):
                return False
            self._official_group_bridge_recover_state['last_attempt_ts'] = now_ts
            self._official_group_bridge_recover_state['last_attempt_at'] = utc_now()
        try:
            result = self._start_official_group_bridge_service(timeout_seconds=max(float(cooldown_seconds), 30.0))
        except Exception as exc:
            with self._official_group_bridge_recover_lock:
                self._official_group_bridge_recover_state['last_error'] = str(exc)
                self._official_group_bridge_recover_state['last_result'] = {'started': False, 'error': str(exc)}
            return False
        with self._official_group_bridge_recover_lock:
            self._official_group_bridge_recover_state['last_error'] = None
            self._official_group_bridge_recover_state['last_result'] = dict(result or {})
        return True

    def _official_group_bridge_summary_payload(self) -> Dict[str, Any]:
        base_url = self._official_group_bridge_console_base_url()
        candidate_base_urls = self._official_group_bridge_candidate_base_urls(base_url)
        if not candidate_base_urls:
            return {'configured': False, 'health': {}, 'summary': {}, 'auto_recovered': False}

        probes = [self._probe_official_group_bridge_base_url(candidate) for candidate in candidate_base_urls]
        for probe in probes:
            if probe.get('ready'):
                return {
                    'configured': True,
                    'base_url': probe['base_url'],
                    'health': probe['health'],
                    'summary': probe['summary'],
                    'auto_recovered': False,
                }

        auto_recovered = self._maybe_auto_recover_official_group_bridge()
        if auto_recovered:
            probes = [self._probe_official_group_bridge_base_url(candidate) for candidate in candidate_base_urls]
            for probe in probes:
                if probe.get('ready'):
                    return {
                        'configured': True,
                        'base_url': probe['base_url'],
                        'health': probe['health'],
                        'summary': probe['summary'],
                        'auto_recovered': True,
                    }

        fallback_probe = probes[0]
        if len(probes) > 1:
            for probe in probes[1:]:
                if probe.get('health', {}).get('status') == 'healthy':
                    fallback_probe = probe
                    break
        return {
            'configured': True,
            'base_url': fallback_probe['base_url'],
            'health': fallback_probe['health'],
            'summary': fallback_probe['summary'],
            'auto_recovered': bool(auto_recovered),
        }

    def _current_local_minutes(self) -> int:
        now = datetime.now().astimezone()
        return (now.hour * 60) + now.minute

    def _schedule_window_contains_minutes(self, start: str, end: str, minute_of_day: int) -> bool:
        start_hour, start_minute = [int(part) for part in start.split(':', 1)]
        end_hour, end_minute = [int(part) for part in end.split(':', 1)]
        start_total = (start_hour * 60) + start_minute
        end_total = (end_hour * 60) + end_minute
        if start_total <= end_total:
            return start_total <= minute_of_day <= end_total
        return minute_of_day >= start_total or minute_of_day <= end_total

    def _schedule_runtime(self, schedule_windows: List[Dict[str, Any]]) -> Dict[str, Any]:
        normalized_windows: List[Dict[str, str]] = []
        for item in schedule_windows or []:
            start = str((item or {}).get('start') or '').strip()
            end = str((item or {}).get('end') or '').strip()
            if re.fullmatch(r'\d{2}:\d{2}', start) and re.fullmatch(r'\d{2}:\d{2}', end):
                normalized_windows.append({'start': start, 'end': end})
        if not normalized_windows:
            return {
                'configured': False,
                'active_now': True,
                'status': 'always_on',
                'label': '未设置时间段（默认全天）',
            }
        current_minute = self._current_local_minutes()
        active_now = any(self._schedule_window_contains_minutes(item['start'], item['end'], current_minute) for item in normalized_windows)
        return {
            'configured': True,
            'active_now': active_now,
            'status': 'active_window' if active_now else 'outside_window',
            'label': '当前时段生效中' if active_now else '当前不在监控时段',
        }

    def _production_ops_daemon_snapshot(self) -> Dict[str, Any]:
        return self.get_production_ops_daemon_config()

    def _production_ops_daemon_snapshot_light(self) -> Dict[str, Any]:
        return self.get_production_ops_daemon_config_light()

    @staticmethod
    def _production_ops_cycle_anchor_maps(production_ops: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, str]]:
        runtime = dict((production_ops or {}).get('runtime') or {})
        state = dict(runtime.get('state') or {})
        registration = dict(state.get('registration_cycle_anchors') or {}) if isinstance(state.get('registration_cycle_anchors'), dict) else {}
        official = dict(state.get('official_cycle_anchors') or {}) if isinstance(state.get('official_cycle_anchors'), dict) else {}
        return {
            'registration_group': {str(key).strip(): str(value).strip() for key, value in registration.items() if str(key).strip() and str(value).strip()},
            'official_group': {str(key).strip(): str(value).strip() for key, value in official.items() if str(key).strip() and str(value).strip()},
        }

    @classmethod
    def _lookup_binding_cycle_anchor(cls, *, production_ops: Optional[Dict[str, Any]], responsible_type: str, binding: Optional[Dict[str, Any]] = None, probe: Optional[Dict[str, Any]] = None) -> Optional[str]:
        normalized_type = str(responsible_type or '').strip()
        if normalized_type not in {'registration_group', 'official_group'}:
            return None
        anchor_map = cls._production_ops_cycle_anchor_maps(production_ops).get(normalized_type) or {}
        if not anchor_map:
            return None
        binding = dict(binding or {})
        probe = dict(probe or {})
        stable_candidate_keys = [
            str(binding.get('registration_group') or '').strip(),
            str(binding.get('group_id') or '').strip(),
            str(binding.get('runtime_probe_group_id') or '').strip(),
            str(probe.get('group_id') or '').strip(),
            str(binding.get('link') or '').strip(),
        ]
        for candidate in stable_candidate_keys:
            if candidate and candidate in anchor_map:
                return anchor_map[candidate]
        return None

    @classmethod
    def _lookup_binding_cycle_anchor_identity(
        cls,
        *,
        production_ops: Optional[Dict[str, Any]],
        responsible_type: str,
        binding: Optional[Dict[str, Any]] = None,
        probe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        normalized_type = str(responsible_type or '').strip()
        if normalized_type not in {'registration_group', 'official_group'}:
            return {}
        anchor_map = cls._production_ops_cycle_anchor_maps(production_ops).get(normalized_type) or {}
        if not anchor_map:
            return {}
        binding = dict(binding or {})
        probe = dict(probe or {})
        stable_candidate_keys = [
            str(binding.get('registration_group') or '').strip(),
            str(binding.get('group_id') or '').strip(),
            str(binding.get('runtime_probe_group_id') or '').strip(),
            str(probe.get('group_id') or '').strip(),
            str(binding.get('link') or '').strip(),
        ]
        anchor_at = ''
        for candidate in stable_candidate_keys:
            if candidate and candidate in anchor_map:
                anchor_at = str(anchor_map.get(candidate) or '').strip()
                break
        if not anchor_at:
            return {}

        candidate_set = {item for item in stable_candidate_keys if item}
        peer_keys = [str(key).strip() for key, value in anchor_map.items() if str(value or '').strip() == anchor_at and str(key).strip()]
        group_id_candidates = list(dict.fromkeys(item for item in peer_keys if _looks_like_whatsapp_group_jid(item)))
        configured_group_id = next((item for item in stable_candidate_keys if _looks_like_whatsapp_group_jid(item)), '')
        if configured_group_id:
            group_id = configured_group_id
        elif len(group_id_candidates) == 1:
            group_id = group_id_candidates[0]
        else:
            group_id = ''

        def readable_name_score(value: str) -> tuple[int, int, str]:
            text = str(value or '').strip()
            if not text or _looks_like_whatsapp_group_jid(text) or _looks_like_whatsapp_invite_link(text):
                return (-100, 0, text)
            score = 0
            if text not in candidate_set:
                score += 8
            if any(ch.isspace() for ch in text):
                score += 4
            if any(ord(ch) > 127 for ch in text):
                score += 3
            if any(mark in text for mark in ('🎃', '🥈', '🥇', 'Grupo', 'Grup', 'Linky')):
                score += 2
            return (score, len(text), text)

        readable_names = list(dict.fromkeys(item for item in peer_keys if readable_name_score(item)[0] > -100))
        group_name = ''
        if len(readable_names) == 1:
            group_name = max(readable_names, key=readable_name_score)
        result: Dict[str, str] = {'cycle_anchor_at': anchor_at}
        if group_id:
            result['group_id'] = group_id
        if group_name:
            result['group_name'] = group_name
        return result

    @staticmethod
    def _binding_probe_from_production_ops_status(
        production_ops: Optional[Dict[str, Any]],
        *,
        responsible_type: str,
        binding: Optional[Dict[str, Any]] = None,
        account_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if str(responsible_type or '').strip() != 'registration_group':
            return {}
        runtime = dict((production_ops or {}).get('runtime') or {})
        status = dict(runtime.get('status') or {})
        binding = dict(binding or {})
        binding_group = str(binding.get('registration_group') or '').strip()
        binding_group_id = str(binding.get('group_id') or '').strip()
        binding_link = str(binding.get('link') or '').strip()
        binding_group_name = str(binding.get('group_name') or '').strip()
        expected_account_key = str(account_key or '').strip()

        def _probe_from_parts(monitor_target: Dict[str, Any], decision_group_state: Dict[str, Any], cycle_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            payload = dict(decision_group_state.get('payload') or {}) if isinstance(decision_group_state.get('payload'), dict) else {}
            cycle_context = dict(cycle_context or {})
            if not payload:
                return {}
            monitor_account_key = str(monitor_target.get('account_key') or cycle_context.get('account_key') or '').strip()
            if expected_account_key and monitor_account_key and monitor_account_key != expected_account_key:
                return {}
            cycle_registration_group = str(cycle_context.get('registration_group') or '').strip()
            cycle_group_id = str(cycle_context.get('group_id') or '').strip()
            cycle_group_name = str(cycle_context.get('group_name') or '').strip()
            cycle_binding_link = str(cycle_context.get('binding_link') or '').strip()
            payload_group_id = str(payload.get('group_id') or '').strip()
            payload_group_name = str(payload.get('group_name') or '').strip()
            expected_config_fingerprint = str(binding.get('config_fingerprint') or '').strip()
            incoming_config_fingerprint = str(
                monitor_target.get('config_fingerprint')
                or cycle_context.get('config_fingerprint')
                or decision_group_state.get('config_fingerprint')
                or payload.get('config_fingerprint')
                or ''
            ).strip()
            if expected_config_fingerprint and incoming_config_fingerprint and incoming_config_fingerprint != expected_config_fingerprint:
                return {}
            monitor_registration_group = str(monitor_target.get('registration_group') or cycle_registration_group).strip()
            # 群链接是配置真源。若 daemon 状态仍是旧 group_id 目标，不能仅因为旧群名相同就复用旧探针结果；
            # 但生产 cycle 的 registration_group 常直接存 group_id，payload 可能不重复带 group_id。
            effective_payload_group_id = str(payload_group_id or cycle_group_id or cycle_registration_group or monitor_target.get('group_id') or '').strip()
            if binding_link and monitor_registration_group and monitor_registration_group != binding_link:
                if not (binding_group_id and effective_payload_group_id and effective_payload_group_id == binding_group_id):
                    return {}
            target_matches_monitor = any([
                bool(binding_group and monitor_registration_group == binding_group),
                bool(binding_group_id and str(payload_group_id or monitor_target.get('group_id') or cycle_group_id or cycle_registration_group).strip() == binding_group_id),
                bool(binding_link and monitor_registration_group == binding_link),
                bool(binding_group_name and not binding_link and str(monitor_target.get('group_name') or monitor_target.get('binding_group_name') or payload_group_name or cycle_group_name).strip() == binding_group_name),
            ])
            if not target_matches_monitor:
                return {}
            if binding_group_id and not payload_group_id:
                payload['group_id'] = binding_group_id
            if binding_group_name and not payload_group_name:
                payload['group_name'] = binding_group_name
            return {
                **payload,
                'source': decision_group_state.get('source') or payload.get('source') or 'production_ops_daemon',
                'source_base_url': str(monitor_target.get('worker_base_url') or cycle_context.get('worker_base_url') or '').strip() or None,
                'config_fingerprint': incoming_config_fingerprint or expected_config_fingerprint or None,
                'probe_target': binding_group_id or binding_group or binding_link or binding_group_name or str(payload.get('group_id') or '').strip() or str(payload.get('group_name') or '').strip(),
                'zero_pending_unverified': bool(decision_group_state.get('zero_pending_unverified', payload.get('zero_pending_unverified'))),
                'zero_pending_unverified_reason': decision_group_state.get('zero_pending_unverified_reason') or payload.get('zero_pending_unverified_reason'),
                'zero_pending_verified_by': decision_group_state.get('zero_pending_verified_by') or payload.get('zero_pending_verified_by'),
                'pending_zero_confidence': decision_group_state.get('pending_zero_confidence') or payload.get('pending_zero_confidence'),
                'probe_data_quality': decision_group_state.get('probe_data_quality') or payload.get('probe_data_quality'),
                'data_quality': decision_group_state.get('data_quality') or payload.get('data_quality'),
                'empty_queue_evidence': decision_group_state.get('empty_queue_evidence') or payload.get('empty_queue_evidence'),
                'empty_queue_visible': bool(decision_group_state.get('empty_queue_visible', payload.get('empty_queue_visible'))),
                'has_pending_section': bool(decision_group_state.get('has_pending_section', payload.get('has_pending_section'))),
                'has_pending_request_row': bool(decision_group_state.get('has_pending_request_row', payload.get('has_pending_request_row'))),
            }

        for cycle in list(status.get('registration_group_cycles') or []):
            if not isinstance(cycle, dict):
                continue
            cycle_probe = _probe_from_parts(
                dict(cycle.get('monitor_target') or {}) if isinstance(cycle.get('monitor_target'), dict) else {},
                dict(cycle.get('decision_group_state') or {}) if isinstance(cycle.get('decision_group_state'), dict) else {},
                cycle,
            )
            if cycle_probe:
                return cycle_probe

        return _probe_from_parts(
            dict(status.get('monitor_target') or {}) if isinstance(status.get('monitor_target'), dict) else {},
            dict(status.get('decision_group_state') or {}) if isinstance(status.get('decision_group_state'), dict) else {},
        )

    @staticmethod
    def _extract_live_group_probe(
        production_ops: Optional[Dict[str, Any]] = None,
        *,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        account_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        expected_account_key = str(account_key or '').strip()
        production_runtime = dict((production_ops or {}).get('runtime') or {})
        production_status = production_runtime.get('status') if isinstance(production_runtime.get('status'), dict) else {}
        monitor_target = dict(production_status.get('monitor_target') or {}) if isinstance(production_status.get('monitor_target'), dict) else {}
        monitor_account_key = str(monitor_target.get('account_key') or '').strip()
        production_matches_account = not expected_account_key or not monitor_account_key or monitor_account_key == expected_account_key
        runtime = dict(runtime_state or (production_runtime if production_matches_account else {}) or {})
        runtime_status = runtime.get('status') if isinstance(runtime.get('status'), dict) else (production_status if production_matches_account else {})
        truth_kwargs = {'status': runtime_status}
        if runtime_state:
            truth_kwargs['runtime_state'] = runtime_state
        if session_state:
            truth_kwargs['session_state'] = session_state
        truth_state = build_truth_state(**truth_kwargs)

        payload = dict(truth_state.get('payload') or {})
        if payload or truth_state.get('group_name') or truth_state.get('group_id'):
            return {
                'source': truth_state.get('source'),
                'group_name': str(payload.get('group_name') or truth_state.get('group_name') or '').strip(),
                'group_id': str(payload.get('group_id') or truth_state.get('group_id') or '').strip(),
                'pending_count': payload.get('pending_count', truth_state.get('pending_count')),
                'member_count': payload.get('member_count', truth_state.get('member_count')),
                'requester_ids': list(payload.get('requester_ids') or truth_state.get('requester_ids') or []),
                'requesters': list(payload.get('requesters') or truth_state.get('requesters') or []),
                'zero_pending_unverified': bool(truth_state.get('zero_pending_unverified')),
                'zero_pending_unverified_reason': truth_state.get('zero_pending_unverified_reason'),
                'zero_pending_verified_by': truth_state.get('zero_pending_verified_by'),
                'pending_zero_confidence': truth_state.get('pending_zero_confidence'),
                'data_quality': truth_state.get('data_quality'),
                'session_health': truth_state.get('session_health'),
                'source_ts': truth_state.get('source_ts'),
                'empty_queue_visible': bool(truth_state.get('empty_queue_visible')),
                'truth_state': truth_state,
            }
        return {
            'source': None,
            'group_name': '',
            'group_id': '',
            'pending_count': None,
            'member_count': None,
            'requester_ids': [],
            'requesters': [],
            'zero_pending_unverified': False,
            'pending_zero_confidence': None,
            'truth_state': truth_state,
        }

    @staticmethod
    def _truth_state_from_probe_payload(
        probe_payload: Optional[Dict[str, Any]],
        *,
        source: Optional[str] = None,
        runtime_state: Optional[Dict[str, Any]] = None,
        session_state: Optional[Dict[str, Any]] = None,
        monitor_target: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(probe_payload or {})
        if not payload:
            return build_truth_state(
                status={},
                runtime_state=runtime_state,
                session_state=session_state,
                monitor_target=monitor_target,
            )
        decision_group_state = {
            'payload': payload,
            'source': str(source or payload.get('source') or payload.get('source_base_url') or 'binding_probe').strip() or 'binding_probe',
            'zero_pending_unverified': bool(payload.get('zero_pending_unverified')),
            'zero_pending_unverified_reason': payload.get('zero_pending_unverified_reason'),
            'zero_pending_verified_by': payload.get('zero_pending_verified_by'),
        }
        return build_truth_state(
            status={'decision_group_state': decision_group_state},
            runtime_state=runtime_state,
            session_state=session_state,
            monitor_target=monitor_target,
        )

    @staticmethod
    def _membership_verifier_gate_from_truth_state(
        truth_state: Optional[Dict[str, Any]],
        *,
        probe: Optional[Dict[str, Any]] = None,
        source_fallback: Optional[str] = None,
        include_unconfirmed_probe_unavailable: bool = False,
        probe_unavailable_detail: str = '实时探针结果证据不足，暂不能判定真实群状态。',
    ) -> Optional[Dict[str, Any]]:
        normalized_truth_state = dict(truth_state or {})
        if not normalized_truth_state:
            return None
        normalized_probe = dict(probe or {})
        truth_status = str(normalized_truth_state.get('status') or '').strip()
        source = normalized_truth_state.get('source') or source_fallback
        if truth_status == 'session_mismatch':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'session_mismatch',
                'detail': '当前会话与目标账号不一致，需重建会话后再使用。',
                'source': source,
                'probe': normalized_probe,
                'truth_state': normalized_truth_state,
            }
        if truth_status == 'runtime_unhealthy':
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'runtime_unavailable',
                'detail': '未就绪',
                'source': source,
                'probe': normalized_probe,
                'truth_state': normalized_truth_state,
            }
        if truth_status == 'empty_unverified':
            detail = '零待审批待核验：探针未看到明确空队列证据，暂不作为真实群状态。'
            pending_count = normalized_truth_state.get('pending_count')
            try:
                pending_value = int(pending_count)
            except (TypeError, ValueError):
                pending_value = None
            if pending_value is not None:
                detail = f'零待审批待核验：当前探针读数 {pending_value} 人，但未看到明确空队列证据，暂不作为真实群状态。'
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'zero_pending_unverified',
                'detail': detail,
                'source': source,
                'probe': normalized_probe,
                'truth_state': normalized_truth_state,
            }
        if include_unconfirmed_probe_unavailable and truth_status and truth_status not in {'confirmed_pending', 'confirmed_empty'}:
            return {
                'ready': False,
                'requires_manual_seed': True,
                'status': 'probe_unavailable',
                'detail': probe_unavailable_detail,
                'source': source,
                'probe': normalized_probe,
                'truth_state': normalized_truth_state,
            }
        return None

    @staticmethod
    def _baileys_session_has_explicit_login_block(session_state: Optional[Dict[str, Any]]) -> bool:
        session = dict(session_state or {})
        blocked_statuses = {
            'account_restricted',
            'auth_failed',
            'needs_scan',
            'qr_expired',
            'qr_pending',
            'session_mismatch',
            'waiting_for_scan',
        }
        blocked_states = {
            'account_restricted',
            'disabled',
            'login_failed',
            'qr_expired',
            'runtime_stopped',
            'runtime_unhealthy',
            'session_mismatch',
            'waiting_for_scan_qr_pending',
            'waiting_for_scan_qr_ready',
        }
        status = str(session.get('login_check_status') or '').strip()
        login_state = str(session.get('login_state') or '').strip()
        return bool(
            status in blocked_statuses
            or login_state in blocked_states
            or session.get('qr_available') is True
            or session.get('can_show_qr') is True
            or session.get('qr_stale') is True
        )

    @staticmethod
    def _baileys_session_can_be_marked_operational(
        session_state: Optional[Dict[str, Any]],
        runtime_state: Optional[Dict[str, Any]] = None,
    ) -> bool:
        session = dict(session_state or {})
        runtime = dict(runtime_state or {})
        blocked_statuses = {'account_restricted', 'auth_failed', 'session_mismatch'}
        status = str(session.get('login_check_status') or '').strip()
        login_state = str(session.get('login_state') or '').strip()
        return (
            runtime.get('ready') is True
            and runtime.get('authenticated') is True
            and runtime.get('provider_ready') is True
            and session.get('login_verified') is True
            and session.get('authenticated') is True
            and session.get('ready') is True
            and session.get('can_probe') is True
            and session.get('session_target_match') is not False
            and status not in blocked_statuses
            and login_state not in blocked_statuses
            and not Service._baileys_session_has_explicit_login_block(session)
        )

    @staticmethod
    def _mark_baileys_session_operational(
        runtime_state: Optional[Dict[str, Any]],
        session_state: Optional[Dict[str, Any]],
        *,
        message: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        runtime = dict(runtime_state or {})
        session = dict(session_state or {})
        runtime.update({
            'active': True,
            'ready': True,
            'authenticated': True,
            'provider_ready': True,
            'status': 'running',
            'status_text': 'Baileys 已通过实际探针验证',
        })
        session.update({
            'ready': True,
            'authenticated': True,
            'bound': True,
            'login_verified': True,
            'can_probe': True,
            'qr_available': False,
            'can_show_qr': False,
            'login_check_status': 'passed',
            'login_check_message': message,
            'login_state': 'logged_in',
            'login_state_label': '已登录',
            'login_action': 'none',
        })
        if session.get('session_target_match') is not False:
            session['session_target_match'] = True
        if runtime.get('session_target_match') is not False:
            runtime['session_target_match'] = True
        return runtime, session

    @staticmethod
    def _iso_timestamp_within(value: Any, *, max_age_seconds: float) -> bool:
        raw = str(value or '').strip()
        if not raw:
            return False
        try:
            age_seconds = (datetime.now(timezone.utc) - parse_iso_datetime(raw)).total_seconds()
        except Exception:
            return False
        return bool(age_seconds <= max(float(max_age_seconds or 0.0), 0.0))

    @staticmethod
    def _binding_probe_has_group_evidence(probe: Optional[Dict[str, Any]]) -> bool:
        payload = dict(probe or {})
        return bool(
            str(payload.get('group_id') or payload.get('runtime_probe_group_id') or '').strip()
            or str(payload.get('group_name') or payload.get('runtime_probe_group_name') or '').strip()
            or normalize_int_or_none(payload.get('member_count')) is not None
            or normalize_int_or_none(payload.get('last_probe_member_count')) is not None
            or normalize_int_or_none(payload.get('pending_count')) is not None
            or payload.get('participants_load_status')
            or payload.get('self_participant_found') is not None
            or payload.get('approval_action_visible') is not None
        )

    @staticmethod
    def _stored_binding_probe_payload(binding: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        payload = dict(binding or {})
        approval_queue_truth = dict(payload.get('approval_queue_truth') or {}) if isinstance(payload.get('approval_queue_truth'), dict) else {}
        current_truth = dict(approval_queue_truth.get('current_truth') or approval_queue_truth.get('current_truth_raw') or {}) if isinstance(approval_queue_truth.get('current_truth') or approval_queue_truth.get('current_truth_raw'), dict) else {}
        member_count = normalize_int_or_none(current_truth.get('member_count'))
        if member_count is None:
            member_count = normalize_int_or_none(approval_queue_truth.get('member_count'))
        if member_count is None:
            member_count = normalize_int_or_none(payload.get('last_probe_member_count'))
        if member_count is None:
            member_count = normalize_int_or_none(payload.get('member_count'))
        pending_count = normalize_int_or_none(current_truth.get('pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(approval_queue_truth.get('pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(payload.get('last_probe_pending_count'))
        if pending_count is None:
            pending_count = normalize_int_or_none(payload.get('next_approval_pending_count'))
        requesters = list(current_truth.get('requesters') or approval_queue_truth.get('requesters') or []) if isinstance(current_truth.get('requesters') or approval_queue_truth.get('requesters'), list) else []
        requester_ids = list(current_truth.get('requester_ids') or approval_queue_truth.get('requester_ids') or []) if isinstance(current_truth.get('requester_ids') or approval_queue_truth.get('requester_ids'), list) else []
        if not requester_ids and requesters:
            for requester in requesters:
                if not isinstance(requester, dict):
                    continue
                requester_id = str(requester.get('requesterId') or requester.get('requester_id') or requester.get('id') or '').strip()
                if requester_id:
                    requester_ids.append(requester_id)
        return {
            'source': 'stored_baileys_binding_probe',
            'group_id': str(payload.get('runtime_probe_group_id') or payload.get('group_id') or payload.get('registration_group') or '').strip(),
            'group_name': str(payload.get('runtime_probe_group_name') or payload.get('group_name') or '').strip(),
            'pending_count': pending_count,
            'member_count': member_count,
            'requester_ids': requester_ids,
            'requesters': requesters,
            'review_surface_ready': current_truth.get('review_surface_ready', approval_queue_truth.get('review_surface_ready')),
            'empty_queue_visible': bool(current_truth.get('empty_queue_visible', approval_queue_truth.get('empty_queue_visible', False))),
            'has_pending_section': bool(current_truth.get('has_pending_section', approval_queue_truth.get('has_pending_section', False))),
            'has_pending_request_row': bool(current_truth.get('has_pending_request_row', approval_queue_truth.get('has_pending_request_row', False))),
            'participants_load_status': payload.get('participants_load_status'),
            'self_participant_found': payload.get('last_probe_self_participant_found'),
            'self_is_admin': payload.get('last_probe_self_is_admin'),
            'can_manage_membership_requests': payload.get('last_probe_can_manage_membership_requests'),
            'source_ts': str(payload.get('last_probe_at') or '').strip() or None,
        }


__all__ = ['TimoServiceMixin']

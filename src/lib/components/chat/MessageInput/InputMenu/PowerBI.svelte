<script lang="ts">
	import { onDestroy, onMount, tick, getContext } from 'svelte';

	import { WEBUI_BASE_URL } from '$lib/constants';
	import {
		getPowerBIStatus,
		searchPowerBIWorkspaces,
		searchPowerBIWorkspaceDatasets
	} from '$lib/apis/powerbi';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import ChartBar from '$lib/components/icons/ChartBar.svelte';
	import Folder from '$lib/components/icons/Folder.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Loader from '$lib/components/common/Loader.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import SearchInput from './SearchInput.svelte';

	const i18n = getContext('i18n');

	export let onSelect = (e) => {};

	let loaded = false;
	let selectedIdx = 0;
	let query = '';

	let status = null;

	let selectedWorkspace = null;

	let selectedWorkspaceDatasets = null;
	let selectedWorkspaceDatasetsTotal = null;
	let selectedWorkspaceRequestId = 0;

	$: if (selectedWorkspace) {
		initSelectedWorkspaceDatasets();
	}

	const initSelectedWorkspaceDatasets = async () => {
		selectedWorkspaceRequestId += 1;
		const activeRequestId = selectedWorkspaceRequestId;

		selectedWorkspaceDatasets = null;
		selectedWorkspaceDatasetsTotal = null;
		await tick();

		if (!selectedWorkspace) return;
		const res = await searchPowerBIWorkspaceDatasets(
			localStorage.token,
			selectedWorkspace.id,
			query.trim() || null
		).catch(() => {
			return null;
		});
		if (activeRequestId !== selectedWorkspaceRequestId) return;

		if (res) {
			selectedWorkspaceDatasets = res.items ?? [];
			selectedWorkspaceDatasetsTotal = res.total ?? (res.items ?? []).length;
		} else {
			selectedWorkspaceDatasets = [];
			selectedWorkspaceDatasetsTotal = 0;
		}
	};

	let page = 1;
	let items = [];
	let total = null;
	let limit = null;

	let itemsLoading = false;
	let allItemsLoaded = false;
	let initialized = false;
	let searchedQuery = '';
	let searchDebounceTimer: ReturnType<typeof setTimeout>;
	let requestId = 0;

	// Only re-run the search when the query actually changes. Flipping `initialized`
	// after the initial load would otherwise trigger a second, identical request.
	$: if (initialized && query !== searchedQuery) {
		scheduleSearch();
	}

	const scheduleSearch = () => {
		clearTimeout(searchDebounceTimer);
		searchDebounceTimer = setTimeout(init, 200);
	};

	const init = async () => {
		requestId += 1;
		searchedQuery = query;
		reset();
		selectedWorkspace = null;
		await tick();
		await getItemsPage(requestId);
	};

	const reset = () => {
		page = 1;
		items = [];
		total = null;
		allItemsLoaded = false;
		itemsLoading = false;
	};

	const loadMoreItems = async () => {
		if (allItemsLoaded) return;
		page += 1;
		await getItemsPage(requestId);
	};

	const getItemsPage = async (activeRequestId = requestId) => {
		itemsLoading = true;
		const res = await searchPowerBIWorkspaces(
			localStorage.token,
			query.trim() || null,
			page
		).catch(() => {
			return null;
		});
		if (activeRequestId !== requestId) return res;

		if (res) {
			total = res.total ?? null;
			limit = res.limit ?? null;
			const pageItems = res.items ?? [];

			if (items) {
				const existingIds = new Set(items.map((item) => item.id));
				const newItems = pageItems.filter((item) => !existingIds.has(item.id));
				items = [...items, ...newItems];
			} else {
				items = pageItems;
			}

			// The Power BI API does not always report a total count, so also treat a
			// short page as the end of the list.
			allItemsLoaded =
				pageItems.length === 0 ||
				(total !== null && items.length >= total) ||
				(limit !== null && pageItems.length < limit);
		}

		itemsLoading = false;
		return res;
	};

	const connectHandler = () => {
		window.open(`${WEBUI_BASE_URL}/oauth/clients/powerbi/authorize`, '_self', 'noopener');
	};

	const selectDataset = (workspace, dataset) => {
		onSelect(
			{
				type: 'powerbi_dataset',
				id: dataset.id,
				name: dataset.name,
				workspace_id: workspace.id,
				workspace_name: workspace.name,
				status: 'processed'
			},
			status
		);
	};

	onMount(async () => {
		status = await getPowerBIStatus(localStorage.token).catch(() => {
			return null;
		});

		if (status?.connected) {
			await init();
			await tick();
			initialized = true;
		}
		loaded = true;
	});

	onDestroy(() => {
		clearTimeout(searchDebounceTimer);
	});
</script>

{#if loaded}
	{#if !status?.enabled || !status?.connected}
		<div class="flex flex-col items-center gap-2 px-2 py-4">
			<div class="text-center text-sm text-gray-500 dark:text-gray-400">
				{#if status?.enabled}
					{$i18n.t('Connect your Power BI account to browse your workspaces and datasets.')}
				{:else}
					{$i18n.t('Power BI integration is not enabled.')}
				{/if}
			</div>

			{#if status?.enabled}
				<button
					class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
					type="button"
					on:click={connectHandler}
				>
					{$i18n.t('Connect Power BI')}
				</button>
			{/if}
		</div>
	{:else}
		<div class="flex min-h-0 flex-1 flex-col gap-0.5 overflow-hidden">
			<SearchInput bind:value={query} placeholder={$i18n.t('Search Workspaces')} />

			<div class="min-h-0 flex-1 overflow-y-auto overflow-x-hidden scrollbar-thin">
				{#if items.length === 0 && itemsLoading}
					<div class="py-4.5">
						<Spinner />
					</div>
				{:else if items.length === 0}
					<div class="py-4 text-center text-sm text-gray-500 dark:text-gray-400">
						{$i18n.t('No workspaces found.')}
					</div>
				{:else}
					{#each items as item, idx (item.id)}
						<div
							class=" h-[1.6875rem] px-2 rounded-xl w-full text-left flex justify-between items-center text-[13px] font-normal hover:bg-gray-50/40 hover:text-gray-900 dark:hover:bg-gray-800/40 dark:hover:text-gray-100 {idx ===
							selectedIdx
								? ' bg-gray-50/40 dark:bg-gray-800/40 dark:text-gray-100 selected-command-option-button'
								: ''}"
						>
							<button
								class="w-full flex-1"
								type="button"
								on:click={() => {
									if (selectedWorkspace && selectedWorkspace.id === item.id) {
										selectedWorkspace = null;
									} else {
										selectedWorkspace = item;
									}
								}}
								on:mousemove={() => {
									selectedIdx = idx;
								}}
								on:mouseleave={() => {
									if (idx === 0) {
										selectedIdx = -1;
									}
								}}
								data-selected={idx === selectedIdx}
							>
								<div class="w-full text-left text-black dark:text-gray-100 flex items-center gap-1">
									<Tooltip content={$i18n.t('Workspace')} placement="top">
										<Folder className="size-3.5" />
									</Tooltip>

									<Tooltip
										content={item?.name}
										placement="top-start"
										className="flex flex-1 min-w-0"
									>
										<div class="line-clamp-1 flex-1 text-[13px]">
											{item?.name}
										</div>
									</Tooltip>
								</div>
							</button>

							<Tooltip content={$i18n.t('Show Datasets')} placement="top">
								<button
									type="button"
									class=" ml-2 opacity-50 hover:opacity-100 transition"
									on:click={() => {
										if (selectedWorkspace && selectedWorkspace.id === item.id) {
											selectedWorkspace = null;
										} else {
											selectedWorkspace = item;
										}
									}}
								>
									{#if selectedWorkspace && selectedWorkspace.id === item.id}
										<ChevronDown className="size-3" />
									{:else}
										<ChevronRight className="size-3" />
									{/if}
								</button>
							</Tooltip>
						</div>

						{#if selectedWorkspace && selectedWorkspace.id === item.id}
							<div class="pl-3 mb-1 flex flex-col gap-0.5">
								{#if selectedWorkspaceDatasets === null}
									<div class=" py-1 flex justify-center">
										<Spinner className="size-3" />
									</div>
								{:else if selectedWorkspaceDatasetsTotal === 0}
									<div class=" text-xs text-gray-500 dark:text-gray-400 italic py-0.5 px-2">
										{$i18n.t('No datasets in this workspace.')}
									</div>
								{:else}
									{#each selectedWorkspaceDatasets as dataset (dataset.id)}
										<button
											class=" h-[1.6875rem] px-2 rounded-xl w-full text-left flex justify-between items-center text-[13px] font-normal hover:bg-gray-50/40 hover:text-gray-900 dark:hover:bg-gray-800/40 dark:hover:text-gray-100"
											type="button"
											on:click={() => {
												selectDataset(item, dataset);
											}}
										>
											<div class=" flex items-center gap-1.5">
												<Tooltip content={$i18n.t('Power BI Dataset')} placement="top">
													<ChartBar className="size-3.5" />
												</Tooltip>

												<Tooltip content={dataset?.name} placement="top-start">
													<div class="line-clamp-1 flex-1 text-[13px]">
														{dataset?.name}
													</div>
												</Tooltip>
											</div>
										</button>
									{/each}
								{/if}
							</div>
						{/if}
					{/each}

					{#if !allItemsLoaded}
						<Loader
							on:visible={(e) => {
								if (!itemsLoading) {
									loadMoreItems();
								}
							}}
						>
							<div class="w-full flex justify-center py-4 text-xs animate-pulse items-center gap-2">
								<Spinner className=" size-4" />
								<div class=" ">{$i18n.t('Loading...')}</div>
							</div>
						</Loader>
					{/if}
				{/if}
			</div>
		</div>
	{/if}
{:else}
	<div class="py-4.5">
		<Spinner />
	</div>
{/if}

package org.jellyfin.androidtv.ui.home

import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import androidx.leanback.app.RowsSupportFragment
import androidx.leanback.widget.ListRow
import androidx.leanback.widget.OnItemViewClickedListener
import androidx.leanback.widget.OnItemViewSelectedListener
import androidx.leanback.widget.Presenter
import androidx.leanback.widget.Row
import androidx.leanback.widget.RowPresenter
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.flowWithLifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.launchIn
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.jellyfin.androidtv.auth.repository.UserRepository
import org.jellyfin.androidtv.constant.CustomMessage
import org.jellyfin.androidtv.constant.HomeSectionType
import org.jellyfin.sdk.model.api.ItemSortBy
import org.jellyfin.androidtv.constant.LiveTvOption
import org.jellyfin.androidtv.constant.QueryType
import org.jellyfin.androidtv.data.model.DataRefreshService
import org.jellyfin.androidtv.data.repository.CustomMessageRepository
import org.jellyfin.androidtv.data.repository.NotificationsRepository
import org.jellyfin.androidtv.data.repository.UserViewsRepository
import org.jellyfin.androidtv.data.service.BackgroundService
import org.jellyfin.androidtv.preference.UserPreferences
import org.jellyfin.androidtv.preference.UserSettingPreferences
import org.jellyfin.androidtv.ui.GridButton
import android.widget.Toast
import org.jellyfin.androidtv.ui.browsing.CompositeClickedListener
import org.jellyfin.androidtv.ui.everflix.DiscoverAsked
import org.jellyfin.androidtv.ui.everflix.DiscoverTitle
import org.jellyfin.androidtv.ui.everflix.HomeFragmentDiscoverRow
import org.jellyfin.androidtv.ui.everflix.Videotecario
import org.jellyfin.androidtv.ui.browsing.CompositeSelectedListener
import org.jellyfin.androidtv.ui.itemhandling.BaseRowItem
import org.jellyfin.androidtv.ui.itemhandling.ItemLauncher
import org.jellyfin.androidtv.ui.itemhandling.ItemRowAdapter
import org.jellyfin.androidtv.ui.itemhandling.refreshItem
import org.jellyfin.androidtv.ui.navigation.Destinations
import org.jellyfin.androidtv.ui.navigation.NavigationRepository
import org.jellyfin.androidtv.ui.playback.AudioEventListener
import org.jellyfin.androidtv.ui.playback.MediaManager
import org.jellyfin.androidtv.ui.presentation.CardPresenter
import org.jellyfin.androidtv.ui.presentation.MutableObjectAdapter
import org.jellyfin.androidtv.ui.presentation.PositionableListRowPresenter
import org.jellyfin.androidtv.util.KeyProcessor
import org.jellyfin.playback.core.PlaybackManager
import org.jellyfin.sdk.api.client.ApiClient
import org.jellyfin.sdk.api.client.extensions.itemsApi
import org.jellyfin.sdk.model.api.BaseItemKind
import org.jellyfin.sdk.model.api.request.GetItemsRequest
import org.jellyfin.sdk.api.sockets.subscribe
import org.jellyfin.sdk.model.api.LibraryChangedMessage
import org.jellyfin.sdk.model.api.UserDataChangedMessage
import org.koin.android.ext.android.inject
import timber.log.Timber
import kotlin.time.Duration.Companion.seconds

class HomeRowsFragment : RowsSupportFragment(), AudioEventListener, View.OnKeyListener {
	companion object {
		private var lastGenres: List<String>? = null
	}

	private val api by inject<ApiClient>()
	private val backgroundService by inject<BackgroundService>()
	private val playbackManager by inject<PlaybackManager>()
	private val mediaManager by inject<MediaManager>()
	private val notificationsRepository by inject<NotificationsRepository>()
	private val userRepository by inject<UserRepository>()
	private val userPreferences by inject<UserPreferences>()
	private val userSettingPreferences by inject<UserSettingPreferences>()
	private val userViewsRepository by inject<UserViewsRepository>()
	private val dataRefreshService by inject<DataRefreshService>()
	private val customMessageRepository by inject<CustomMessageRepository>()
	private val navigationRepository by inject<NavigationRepository>()
	private val itemLauncher by inject<ItemLauncher>()
	private val keyProcessor by inject<KeyProcessor>()

	private val helper by lazy { HomeFragmentHelper(requireContext(), userRepository, userPreferences) }

	// Data
	private var currentItem: BaseRowItem? = null
	private var currentRow: ListRow? = null
	private var justLoaded = true

	// Special rows
	private val notificationsRow by lazy { NotificationsHomeFragmentRow(lifecycleScope, notificationsRepository) }
	private val nowPlaying by lazy { HomeFragmentNowPlayingRow(lifecycleScope, playbackManager, mediaManager) }

	override fun onCreate(savedInstanceState: Bundle?) {
		super.onCreate(savedInstanceState)

		adapter = MutableObjectAdapter<Row>(PositionableListRowPresenter())

		lifecycleScope.launch(Dispatchers.IO) {
			val currentUser = withTimeout(30.seconds) {
				userRepository.currentUser.filterNotNull().first()
			}

			// Start out with default sections
			val homesections = userSettingPreferences.activeHomesections

			// Make sure the rows are empty
			val rows = mutableListOf<HomeFragmentRow>()

			// Check for coroutine cancellation
			if (!isActive) return@launch

			// Everflix home: what you're watching, picks from your library by what you watch most,
			// what just arrived, and then what's out there that you may not have yet
			rows.add(helper.loadResumeVideo())
			rows.add(helper.loadNextUp())
			// Remembered from last time so the home screen never waits for it; refreshed in the background
			val topGenres = lastGenres ?: kotlinx.coroutines.withTimeoutOrNull(1500) { runCatching { helper.favouriteGenres(api) }.getOrNull() }.orEmpty()
			if (lastGenres != null) launch { runCatching { helper.favouriteGenres(api) }.onSuccess { lastGenres = it } }
			else lastGenres = topGenres.takeIf { it.isNotEmpty() }
			if (topGenres.isNotEmpty()) rows.add(helper.loadForYou("Recomendado para ti", ItemSortBy.COMMUNITY_RATING, unwatchedOnly = true, genres = topGenres))
			// Without a viewing history: a slice of what you haven't seen that moves on each day
			else rows.add(helper.loadForYou("Recomendado para ti", ItemSortBy.PREMIERE_DATE, unwatchedOnly = true, startIndex = (java.time.LocalDate.now().dayOfYear * 7) % 120))
			rows.add(helper.loadForYou("Lo último en tu biblioteca", ItemSortBy.DATE_CREATED, unwatchedOnly = false))
			rows.add(helper.loadForYou("Mi lista", ItemSortBy.DATE_CREATED, favoritesOnly = true))
			if (currentUser.policy?.enableLiveTvAccess == true && HomeSectionType.LIVE_TV in homesections) {
				rows.add(HomeFragmentLiveTVRow(requireActivity(), userRepository))
				rows.add(helper.loadOnNow())
			}

			// Add sections to layout
			withContext(Dispatchers.Main) {
				val cardPresenter = CardPresenter()

				// Add rows in order
				notificationsRow.addToRowsAdapter(requireContext(), cardPresenter, adapter as MutableObjectAdapter<Row>)
				nowPlaying.addToRowsAdapter(requireContext(), cardPresenter, adapter as MutableObjectAdapter<Row>)
				for (row in rows) row.addToRowsAdapter(requireContext(), cardPresenter, adapter as MutableObjectAdapter<Row>)

				// Wire up Live TV sibling rows so the On Now row removes the buttons row when empty
				@Suppress("UNCHECKED_CAST")
				val rowsAdapter = adapter as MutableObjectAdapter<Row>
				for (i in 0 until rowsAdapter.size()) {
					val listRow = rowsAdapter.get(i) as? ListRow ?: continue
					val itemAdapter = listRow.adapter as? ItemRowAdapter ?: continue
					if (itemAdapter.queryType == QueryType.LiveTvProgram && i > 0) {
						val previousRow = rowsAdapter.get(i - 1)
						if (previousRow != null) itemAdapter.setSiblingRow(previousRow)
					}
				}
			}

			// Rows from outside the library come last and load after the rest, so they never slow the home screen down
			val admin = currentUser.policy?.isAdministrator == true
			val videotecario = Videotecario(api.baseUrl, api.accessToken)
			// The first time Videotecario may still be preparing these rows: try again once
			val discoverRows = videotecario.home().ifEmpty { delay(5.seconds); videotecario.home() }
			withContext(Dispatchers.Main) {
				val cardPresenter = CardPresenter()
				for (row in discoverRows) HomeFragmentDiscoverRow(row, admin).addToRowsAdapter(requireContext(), cardPresenter, adapter as MutableObjectAdapter<Row>)
			}
		}

		onItemViewClickedListener = CompositeClickedListener().apply {
			registerListener(ItemViewClickedListener())
			registerListener(notificationsRow::onItemClicked)
		}

		onItemViewSelectedListener = CompositeSelectedListener().apply {
			registerListener(ItemViewSelectedListener())
		}

		customMessageRepository.message
			.flowWithLifecycle(lifecycle, Lifecycle.State.RESUMED)
			.onEach { message ->
				when (message) {
					CustomMessage.RefreshCurrentItem -> refreshCurrentItem()
					else -> Unit
				}
			}.launchIn(lifecycleScope)

		lifecycleScope.launch {
			lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
				api.webSocket.subscribe<UserDataChangedMessage>()
					.onEach { refreshRows(force = true, delayed = false) }
					.launchIn(this)

				api.webSocket.subscribe<LibraryChangedMessage>()
					.onEach { refreshRows(force = true, delayed = false) }
					.launchIn(this)
			}
		}

		// Subscribe to Audio messages
		mediaManager.addAudioEventListener(this)
	}

	override fun onKey(v: View?, keyCode: Int, event: KeyEvent?): Boolean {
		if (event?.action != KeyEvent.ACTION_UP) return false
		return keyProcessor.handleKey(keyCode, currentItem, activity)
	}

	override fun onResume() {
		super.onResume()

		//React to deletion
		if (currentRow != null && currentItem != null && currentItem?.baseItem != null && currentItem!!.baseItem!!.id == dataRefreshService.lastDeletedItemId) {
			(currentRow!!.adapter as ItemRowAdapter).remove(currentItem)
			currentItem = null
			dataRefreshService.lastDeletedItemId = null
		}

		if (!justLoaded) {
			//Re-retrieve anything that needs it but delay slightly so we don't take away gui landing
			refreshCurrentItem()
			refreshRows()
		} else {
			justLoaded = false
		}

		// Update audio queue
		Timber.i("Updating audio queue in HomeFragment (onResume)")
		nowPlaying.update(requireContext(), adapter as MutableObjectAdapter<Row>)
	}

	override fun onQueueStatusChanged(hasQueue: Boolean) {
		if (activity == null || requireActivity().isFinishing) return

		Timber.i("Updating audio queue in HomeFragment (onQueueStatusChanged)")
		nowPlaying.update(requireContext(), adapter as MutableObjectAdapter<Row>)
	}

	private fun refreshRows(force: Boolean = false, delayed: Boolean = true) {
		lifecycleScope.launch(Dispatchers.IO) {
			if (delayed) delay(1.5.seconds)

			repeat(adapter.size()) { i ->
				val rowAdapter = (adapter[i] as? ListRow)?.adapter as? ItemRowAdapter
				if (force) rowAdapter?.Retrieve()
				else rowAdapter?.ReRetrieveIfNeeded()
			}
		}
	}

	private fun refreshCurrentItem() {
		val adapter = currentRow?.adapter as? ItemRowAdapter ?: return
		val item = currentItem ?: return

		Timber.i("Refresh item ${item.getFullName(requireContext())}")
		adapter.refreshItem(api, this, item)
	}

	override fun onDestroy() {
		super.onDestroy()

		mediaManager.removeAudioEventListener(this)
	}

	/** Already in the library: open it. Not yet: ask first, then download (admin) or request it (everyone else). */
	private fun onDiscoverClicked(title: DiscoverTitle) {
		if (title.have) {
			lifecycleScope.launch {
				val id = withContext(Dispatchers.IO) { runCatching { findOwned(title) }.getOrNull() }
				if (id != null) navigationRepository.navigate(Destinations.itemDetails(id))
				else navigationRepository.navigate(Destinations.search(title.title))
			}
			return
		}
		val admin = userRepository.currentUser.value?.policy?.isAdministrator == true
		if (DiscoverAsked.key(title) in DiscoverAsked.keys.value) {
			Toast.makeText(requireContext(), (if (admin) "Ya está en Descargas: " else "Ya lo pediste: ") + title.title, Toast.LENGTH_SHORT).show()
			return
		}
		android.app.AlertDialog.Builder(requireContext())
			.setTitle(title.title)
			.setMessage("Esto no está en tu biblioteca. " + if (admin) "¿Quieres descargarlo?" else "¿Quieres pedirlo?")
			.setPositiveButton(if (admin) "Sí, descargar" else "Sí, pedirlo") { _, _ ->
				lifecycleScope.launch {
					val error = Videotecario(api.baseUrl, api.accessToken).ask(title)
					if (error == null) DiscoverAsked.keys.value += DiscoverAsked.key(title)
					val done = if (admin) "A descargar: ${title.title}. Aparecerá sola en tu biblioteca." else "Pedido enviado: ${title.title}. Se descargará cuando se apruebe."
					Toast.makeText(requireContext(), error ?: done, Toast.LENGTH_SHORT).show()
				}
			}
			.setNegativeButton("No", null)
			.show()
	}

	/** The library item for a TheMovieDB title we already have: by its TheMovieDB id, else by name. */
	private suspend fun findOwned(title: DiscoverTitle): java.util.UUID? {
		val kind = if (title.kind == "tv") BaseItemKind.SERIES else BaseItemKind.MOVIE
		val byId by api.itemsApi.getItems(GetItemsRequest(recursive = true, includeItemTypes = setOf(kind), anyProviderIdEquals = listOf("Tmdb.${title.tmdb}"), limit = 1))
		byId.items.firstOrNull()?.let { return it.id }
		val byName by api.itemsApi.getItems(GetItemsRequest(recursive = true, includeItemTypes = setOf(kind), searchTerm = title.title, limit = 1))
		return byName.items.firstOrNull()?.id
	}

	private inner class ItemViewClickedListener : OnItemViewClickedListener {
		override fun onItemClicked(
			itemViewHolder: Presenter.ViewHolder?,
			item: Any?,
			rowViewHolder: RowPresenter.ViewHolder?,
			row: Row?,
		) {
			if (item is GridButton) {
				when (item.id) {
					LiveTvOption.LIVE_TV_GUIDE_OPTION_ID -> navigationRepository.navigate(Destinations.liveTvGuide)
					LiveTvOption.LIVE_TV_SCHEDULE_OPTION_ID -> navigationRepository.navigate(Destinations.liveTvSchedule)
					LiveTvOption.LIVE_TV_RECORDINGS_OPTION_ID -> navigationRepository.navigate(Destinations.liveTvRecordings)
					LiveTvOption.LIVE_TV_SERIES_OPTION_ID -> navigationRepository.navigate(Destinations.liveTvSeriesRecordings)
				}
			}

			if (item is DiscoverTitle) {
				onDiscoverClicked(item)
				return
			}

			if (item !is BaseRowItem) return
			if (row !is ListRow) return
			@Suppress("UNCHECKED_CAST")
			itemLauncher.launch(item, row.adapter as MutableObjectAdapter<Any>, requireContext())
		}
	}

	private inner class ItemViewSelectedListener : OnItemViewSelectedListener {
		override fun onItemSelected(
			itemViewHolder: Presenter.ViewHolder?,
			item: Any?,
			rowViewHolder: RowPresenter.ViewHolder?,
			row: Row?,
		) {
			if (item !is BaseRowItem) {
				currentItem = null
				HomeSelection.item.value = null
				//fill in default background
				backgroundService.clearBackgrounds()
			} else {
				currentItem = item
				currentRow = row as ListRow

				val itemRowAdapter = row.adapter as? ItemRowAdapter
				itemRowAdapter?.loadMoreItemsIfNeeded(itemRowAdapter.indexOf(item))

				backgroundService.setBackground(item.baseItem)
				HomeSelection.item.value = item.baseItem
			}
		}
	}
}

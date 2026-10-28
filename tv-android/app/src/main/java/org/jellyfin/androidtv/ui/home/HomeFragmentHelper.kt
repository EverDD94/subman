package org.jellyfin.androidtv.ui.home

import android.content.Context
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.auth.repository.UserRepository
import org.jellyfin.androidtv.constant.ChangeTriggerType
import org.jellyfin.androidtv.data.repository.ItemRepository
import org.jellyfin.androidtv.preference.UserPreferences
import org.jellyfin.androidtv.ui.browsing.BrowseRowDef
import org.jellyfin.sdk.model.api.BaseItemDto
import org.jellyfin.sdk.api.client.ApiClient
import org.jellyfin.sdk.api.client.extensions.itemsApi
import org.jellyfin.sdk.model.api.BaseItemKind
import org.jellyfin.sdk.model.api.ItemFields
import org.jellyfin.sdk.model.api.ItemSortBy
import org.jellyfin.sdk.model.api.MediaType
import org.jellyfin.sdk.model.api.SortOrder
import org.jellyfin.sdk.model.api.request.GetItemsRequest
import org.jellyfin.sdk.model.api.request.GetNextUpRequest
import org.jellyfin.sdk.model.api.request.GetRecommendedProgramsRequest
import org.jellyfin.sdk.model.api.request.GetRecordingsRequest
import org.jellyfin.sdk.model.api.request.GetResumeItemsRequest
import java.time.LocalDateTime

class HomeFragmentHelper(
	private val context: Context,
	private val userRepository: UserRepository,
	private val userPreferences: UserPreferences,
) {
	fun loadRecentlyAdded(userViews: Collection<BaseItemDto>): HomeFragmentRow {
		return HomeFragmentLatestRow(userRepository, userViews)
	}

	fun loadResume(title: String, includeMediaTypes: Collection<MediaType>): HomeFragmentRow {
		val query = GetResumeItemsRequest(
			limit = ITEM_LIMIT_RESUME,
			fields = ItemRepository.browseFields,
			imageTypeLimit = 1,
			enableTotalRecordCount = false,
			mediaTypes = includeMediaTypes,
			excludeItemTypes = setOf(BaseItemKind.AUDIO_BOOK),
		)

		return HomeFragmentBrowseRowDefRow(BrowseRowDef(title, query, 0, false, true, arrayOf(ChangeTriggerType.TvPlayback, ChangeTriggerType.MoviePlayback)))
	}

	fun loadResumeVideo(): HomeFragmentRow {
		return loadResume(context.getString(R.string.lbl_continue_watching), listOf(MediaType.VIDEO))
	}

	fun loadResumeAudio(): HomeFragmentRow {
		return loadResume(context.getString(R.string.continue_listening), listOf(MediaType.AUDIO))
	}

	fun loadLatestLiveTvRecordings(): HomeFragmentRow {
		val query = GetRecordingsRequest(
			fields = ItemRepository.itemFields,
			enableImages = true,
			limit = ITEM_LIMIT_RECORDINGS
		)

		return HomeFragmentBrowseRowDefRow(BrowseRowDef(context.getString(R.string.lbl_recordings), query))
	}

	fun loadNextUp(): HomeFragmentRow {
		val maxDays = userPreferences[UserPreferences.homeNextUpMaxDays]
		val nextUpDateCutoff = maxDays.takeIf { it > 0 }?.let { LocalDateTime.now().minusDays(it.toLong()) }

		val query = GetNextUpRequest(
			imageTypeLimit = 1,
			limit = ITEM_LIMIT_NEXT_UP,
			// Everflix: keep a half-watched episode here instead of jumping to the next one
			enableResumable = true,
			fields = ItemRepository.browseFields,
			nextUpDateCutoff = nextUpDateCutoff,
		)

		return HomeFragmentBrowseRowDefRow(BrowseRowDef(context.getString(R.string.lbl_next_up), query, arrayOf(ChangeTriggerType.TvPlayback)))
	}

	/** Everflix "Para ti": movies and series mixed in one row, never split by library. */
	fun loadForYou(
		title: String,
		sortBy: ItemSortBy,
		unwatchedOnly: Boolean = false,
		favoritesOnly: Boolean = false,
		startIndex: Int? = null,
		genres: Collection<String>? = null,
		ratedOnly: Boolean = false,
	): HomeFragmentRow {
		val query = GetItemsRequest(
			recursive = true,
			includeItemTypes = setOf(BaseItemKind.SERIES, BaseItemKind.MOVIE),
			isPlayed = if (unwatchedOnly) false else null,
			isFavorite = if (favoritesOnly) true else null,
			startIndex = startIndex,
			genres = genres,
			// Courses and home videos have no rating: they never get recommended
			minCommunityRating = if (ratedOnly) 0.1 else null,
			sortBy = setOf(sortBy),
			sortOrder = setOf(SortOrder.DESCENDING),
			fields = ItemRepository.browseFields,
			imageTypeLimit = 1,
			limit = ITEM_LIMIT_FOR_YOU,
			enableTotalRecordCount = false,
		)

		return HomeFragmentBrowseRowDefRow(BrowseRowDef(title, query, 0, false, true, arrayOf(ChangeTriggerType.LibraryUpdated, ChangeTriggerType.FavoriteUpdate)))
	}

	/** The genres of what you watched lately (movies and the series of your episodes), most watched first. */
	suspend fun favouriteGenres(api: ApiClient): List<String> {
		val played by api.itemsApi.getItems(
			GetItemsRequest(
				recursive = true,
				includeItemTypes = setOf(BaseItemKind.MOVIE, BaseItemKind.EPISODE),
				isPlayed = true,
				sortBy = setOf(ItemSortBy.DATE_PLAYED),
				sortOrder = setOf(SortOrder.DESCENDING),
				fields = setOf(ItemFields.GENRES),
				limit = 80,
				enableTotalRecordCount = false,
			)
		)
		val counts = mutableMapOf<String, Int>()
		played.items.filter { it.type == BaseItemKind.MOVIE }.forEach { m -> m.genres.orEmpty().forEach { counts.merge(it, 1, Int::plus) } }
		// Episodes have no genres of their own: each series counts as many times as you watched it
		val episodesPerSeries = played.items.filter { it.type == BaseItemKind.EPISODE }.mapNotNull { it.seriesId }.groupingBy { it }.eachCount()
		if (episodesPerSeries.isNotEmpty()) {
			val series by api.itemsApi.getItems(GetItemsRequest(ids = episodesPerSeries.keys, fields = setOf(ItemFields.GENRES)))
			series.items.forEach { s -> s.genres.orEmpty().forEach { counts.merge(it, minOf(episodesPerSeries[s.id] ?: 1, 5), Int::plus) } }
		}
		return counts.entries.sortedByDescending { it.value }.take(3).map { it.key }
	}

	fun loadOnNow(): HomeFragmentRow {
		val query = GetRecommendedProgramsRequest(
			isAiring = true,
			fields = ItemRepository.itemFields,
			imageTypeLimit = 1,
			enableTotalRecordCount = false,
			limit = ITEM_LIMIT_ON_NOW
		)

		return HomeFragmentBrowseRowDefRow(BrowseRowDef(context.getString(R.string.lbl_on_now), query))
	}

	companion object {
		// Maximum amount of items loaded for a row
		private const val ITEM_LIMIT_RESUME = 50
		private const val ITEM_LIMIT_RECORDINGS = 40
		private const val ITEM_LIMIT_NEXT_UP = 50
		private const val ITEM_LIMIT_ON_NOW = 20
		private const val ITEM_LIMIT_FOR_YOU = 30
	}
}

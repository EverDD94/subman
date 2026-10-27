package org.jellyfin.androidtv.ui.home

import android.content.Context
import org.jellyfin.androidtv.R
import org.jellyfin.androidtv.auth.repository.UserRepository
import org.jellyfin.androidtv.constant.ChangeTriggerType
import org.jellyfin.androidtv.data.repository.ItemRepository
import org.jellyfin.androidtv.preference.UserPreferences
import org.jellyfin.androidtv.ui.browsing.BrowseRowDef
import org.jellyfin.sdk.model.api.BaseItemDto
import org.jellyfin.sdk.model.api.BaseItemKind
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
	): HomeFragmentRow {
		val query = GetItemsRequest(
			recursive = true,
			includeItemTypes = setOf(BaseItemKind.SERIES, BaseItemKind.MOVIE),
			isPlayed = if (unwatchedOnly) false else null,
			isFavorite = if (favoritesOnly) true else null,
			startIndex = startIndex,
			sortBy = setOf(sortBy),
			sortOrder = setOf(SortOrder.DESCENDING),
			fields = ItemRepository.browseFields,
			imageTypeLimit = 1,
			limit = ITEM_LIMIT_FOR_YOU,
			enableTotalRecordCount = false,
		)

		return HomeFragmentBrowseRowDefRow(BrowseRowDef(title, query, 0, false, true, arrayOf(ChangeTriggerType.LibraryUpdated, ChangeTriggerType.FavoriteUpdate)))
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

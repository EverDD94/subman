package org.jellyfin.androidtv.ui.playback.segment

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.jellyfin.androidtv.preference.UserPreferences
import org.jellyfin.androidtv.ui.everflix.Videotecario
import org.jellyfin.androidtv.util.sdk.duration
import org.jellyfin.sdk.api.client.ApiClient
import org.jellyfin.sdk.api.client.extensions.mediaSegmentsApi
import org.jellyfin.sdk.model.api.BaseItemDto
import org.jellyfin.sdk.model.api.MediaSegmentDto
import org.jellyfin.sdk.model.api.MediaSegmentType
import java.util.UUID
import kotlin.time.Duration.Companion.seconds

interface MediaSegmentRepository {
	companion object {
		/**
		 * All media segments currently supported by the app. The order of these is used for the preferences UI.
		 */
		val SupportedTypes = listOf(
			MediaSegmentType.INTRO,
			MediaSegmentType.OUTRO,
			MediaSegmentType.PREVIEW,
			MediaSegmentType.RECAP,
			MediaSegmentType.COMMERCIAL,
		)

		/**
		 * The minimum duration for a media segment to allow the [MediaSegmentAction.SKIP] action.
		 */
		val SkipMinDuration = 1.seconds

		/**
		 * The minimum duration for a media segment to allow the [MediaSegmentAction.ASK_TO_SKIP] action.
		 */
		val AskToSkipMinDuration = 3.seconds

		/**
		 * The duration to wait before automatically hiding the "ask to skip" UI.
		 */
		val AskToSkipAutoHideDuration = 8.seconds
	}

	fun getDefaultSegmentTypeAction(type: MediaSegmentType): MediaSegmentAction
	fun setDefaultSegmentTypeAction(type: MediaSegmentType, action: MediaSegmentAction)

	suspend fun getSegmentsForItem(item: BaseItemDto): List<MediaSegmentDto>
	fun getMediaSegmentAction(segment: MediaSegmentDto): MediaSegmentAction
}

fun Map<MediaSegmentType, MediaSegmentAction>.toMediaSegmentActionsString() =
	map { "${it.key.serialName}=${it.value.name}" }
		.joinToString(",")

class MediaSegmentRepositoryImpl(
	private val userPreferences: UserPreferences,
	private val api: ApiClient,
) : MediaSegmentRepository {
	private val mediaTypeActions = mutableMapOf<MediaSegmentType, MediaSegmentAction>()

	init {
		restoreMediaTypeActions()
	}

	private fun restoreMediaTypeActions() {
		val restoredMediaTypeActions = userPreferences[UserPreferences.mediaSegmentActions]
			.split(",")
			.mapNotNull {
				runCatching {
					val (type, action) = it.split('=', limit = 2)
					MediaSegmentType.fromName(type) to MediaSegmentAction.valueOf(action)
				}.getOrNull()
			}

		mediaTypeActions.clear()
		mediaTypeActions.putAll(restoredMediaTypeActions)
	}

	private fun saveMediaTypeActions() {
		userPreferences[UserPreferences.mediaSegmentActions] = mediaTypeActions.toMediaSegmentActionsString()
	}

	override fun getDefaultSegmentTypeAction(type: MediaSegmentType): MediaSegmentAction {
		// Always return no action for unsupported types
		if (!MediaSegmentRepository.SupportedTypes.contains(type)) return MediaSegmentAction.NOTHING

		return mediaTypeActions.getOrDefault(type, MediaSegmentAction.NOTHING)
	}

	override fun setDefaultSegmentTypeAction(type: MediaSegmentType, action: MediaSegmentAction) {
		// Don't allow modifying actions for unsupported types
		if (!MediaSegmentRepository.SupportedTypes.contains(type)) return

		mediaTypeActions[type] = action
		saveMediaTypeActions()
	}

	override fun getMediaSegmentAction(segment: MediaSegmentDto): MediaSegmentAction {
		val action = getDefaultSegmentTypeAction(segment.type)
		// Skip the skip action if timespan is too short
		if (action == MediaSegmentAction.SKIP && segment.duration < MediaSegmentRepository.SkipMinDuration) return MediaSegmentAction.NOTHING
		// Skip the ask to skip action if timespan is too short
		if (action == MediaSegmentAction.ASK_TO_SKIP && segment.duration < MediaSegmentRepository.AskToSkipMinDuration) return MediaSegmentAction.NOTHING
		return action
	}

	override suspend fun getSegmentsForItem(item: BaseItemDto): List<MediaSegmentDto> {
		val jellyfin = runCatching {
			withContext(Dispatchers.IO) {
				api.mediaSegmentsApi.getItemSegments(
					itemId = item.id,
					includeSegmentTypes = MediaSegmentRepository.SupportedTypes,
				).content.items
			}
		}.getOrDefault(emptyList())
		// Intro and credits marked by Videotecario replace Intro Skipper's (they are more reliable)
		val ours = runCatching { Videotecario(api.baseUrl, api.accessToken).skips(item.id.toString()) }.getOrDefault(emptyMap())
		if (ours.isEmpty()) return jellyfin
		fun seg(type: MediaSegmentType, range: Pair<Double, Double>) = MediaSegmentDto(
			id = UUID.randomUUID(), itemId = item.id, type = type,
			startTicks = (range.first * 10_000_000).toLong(), endTicks = (range.second * 10_000_000).toLong(),
		)
		val replaced = buildSet {
			if ("intro" in ours) add(MediaSegmentType.INTRO)
			if ("credits" in ours) add(MediaSegmentType.OUTRO)
		}
		return jellyfin.filter { it.type !in replaced } +
			listOfNotNull(ours["intro"]?.let { seg(MediaSegmentType.INTRO, it) }, ours["credits"]?.let { seg(MediaSegmentType.OUTRO, it) })
	}
}
